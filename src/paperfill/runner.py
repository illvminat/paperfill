"""The paper-trading loop over a replayed tape or a recorded stream.

One loop serves both inputs. Each event updates the executor (fills), the portfolio,
the risk engine and the journal, then the strategy is asked for actions, each of which
is judged by the risk engine before it reaches the executor. At the end every open
order is cancelled, positions are settled if the market's payouts are known, and a
`run_end` entry closes the journal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfill.book import OrderBook
from paperfill.execution import Fill, MarketParams, OrderType, PaperExecutor, Portfolio
from paperfill.history import TradePrint
from paperfill.journal import Journal
from paperfill.markets import MarketInfo
from paperfill.recorder import open_text
from paperfill.risk import Intent, Mode, RiskEngine, RiskLimits
from paperfill.signals import FairValue
from paperfill.strategy import Cancel, Context, Quote, Strategy, Take

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class BookEvent:
    ts: datetime
    book: OrderBook


@dataclass(frozen=True, slots=True)
class PriceChangeEvent:
    ts: datetime
    token_id: str
    change: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GapEvent:
    ts: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """An external reference price: source is the RTDS topic suffix."""

    ts: datetime
    source: str  # "binance", "chainlink", "chainlink.twap"
    symbol: str
    value: Decimal


Event = TradePrint | BookEvent | PriceChangeEvent | GapEvent | PriceEvent


def recording_covers_resolution(path: Path, market_end: datetime | None) -> bool:
    """True when the recording's last event is at or after the market's end time.

    Settling a partial recording at the final 1/0 payout would attribute the whole
    resolution to a few seconds of tape; such runs are marked to market instead.
    """
    if market_end is None:
        return False
    last: datetime | None = None
    with open_text(path) as f:
        for line in f:
            if line.strip():
                last = datetime.fromisoformat(json.loads(line)["recv_ts"])
    return last is not None and last >= market_end


def events_from_recording(path: Path) -> Iterator[Event]:
    """Turn a recorder file into events; `last_trade_price` becomes a TradePrint."""
    seq = 0
    with open_text(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            kind, p = r["kind"], r.get("payload", {})
            ts = datetime.fromisoformat(r["recv_ts"])
            if kind == "book":
                yield BookEvent(ts, OrderBook.from_snapshot(p))
            elif kind == "price_change":
                for ch in p["price_changes"]:
                    yield PriceChangeEvent(ts, str(ch.get("token_id") or ch.get("asset_id")), ch)
            elif kind == "last_trade_price":
                yield TradePrint(
                    ts=ts,
                    seq=seq,
                    side=p["side"],
                    price=Decimal(str(p["price"])),
                    size=Decimal(str(p.get("size") or "0")),
                    token_id=str(p.get("token_id") or p.get("asset_id")),
                    outcome=None,
                )
                seq += 1
            elif kind == "gap":
                yield GapEvent(ts, r.get("reason", ""))
            elif kind.startswith("prices.crypto."):
                yield PriceEvent(
                    ts=ts,
                    source=kind.removeprefix("prices.crypto."),
                    symbol=str(p["symbol"]),
                    value=Decimal(str(p["value"])),
                )


@dataclass(slots=True)
class RunConfig:
    capital: Decimal = Decimal("100")
    limits: RiskLimits = field(default_factory=RiskLimits)
    mark_every: timedelta = timedelta(seconds=5)
    kill_file: Path | None = None


@dataclass(slots=True)
class RunResult:
    portfolio: Portfolio
    fills: list[Fill]
    events: int
    halted: str | None
    settled: bool
    payouts: dict[str, Decimal] | None


class Runner:
    def __init__(
        self,
        market: MarketInfo,
        strategy: Strategy,
        journal: Journal,
        config: RunConfig,
        *,
        mode: str,
    ) -> None:
        self.market, self.strategy, self.journal, self.config = market, strategy, journal, config
        self.now = datetime.now(UTC)
        self.executor = PaperExecutor(
            MarketParams(market.fees, market.tick_size, market.min_order_size),
            clock=lambda: self.now,
        )
        self.portfolio = Portfolio(cash=config.capital)
        self.risk = RiskEngine(config.limits, self.portfolio, kill_file=config.kill_file)
        self.risk.token_market = {
            market.yes_token: market.condition_id,
            market.no_token: market.condition_id,
        }
        self.last_prices: dict[str, Decimal] = {}
        self.fair: FairValue | None = (
            FairValue(market.window_start, market.end)
            if market.window_start is not None and market.end is not None
            else None
        )
        self.fills: list[Fill] = []
        self.events = 0
        self.halted: str | None = None
        self._last_mark: datetime | None = None
        self.journal.record(
            "run_start",
            mode=mode,
            market=market.condition_id,
            question=market.question,
            strategy=strategy.name,
            capital=config.capital,
            limits=vars(config.limits)
            if hasattr(config.limits, "__dict__")
            else {k: getattr(config.limits, k) for k in config.limits.__slots__},
            fees={
                "rate": market.fees.rate,
                "exponent": market.fees.exponent,
                "rebate": market.fees.rebate_rate,
            },
            tick=market.tick_size,
            min_order_size=market.min_order_size,
        )

    # -- marks -----------------------------------------------------------------

    def marks(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for token in (self.market.yes_token, self.market.no_token):
            book = self.executor.books.get(token)
            if book is not None and book.mid is not None:
                out[token] = book.mid
            elif token in self.last_prices:
                out[token] = self.last_prices[token]
        return out

    def _mark(self, force: bool = False) -> None:
        if (
            not force
            and self._last_mark is not None
            and self.now - self._last_mark < self.config.mark_every
        ):
            return
        self._last_mark = self.now
        marks = self.marks()
        self.journal.record(
            "mark",
            equity=self.portfolio.equity(marks),
            cash=self.portfolio.cash,
            exposure=self.portfolio.exposure(marks),
            marks=marks,
        )

    # -- event handling ---------------------------------------------------------

    def _apply_fills(self, fills: Iterable[Fill]) -> None:
        fills = list(fills)
        for fill in fills:
            self.portfolio.apply(fill)
            self.fills.append(fill)
            self.journal.record(
                "fill",
                order_id=fill.order_id,
                token=fill.token_id,
                side=fill.side,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
                rebate_estimate=fill.rebate_estimate,
                liquidity=fill.liquidity,
                cash=self.portfolio.cash,
            )
        if fills:
            self._mark(force=True)  # the equity curve must see every fill, not just the clock

    def _handle(self, event: Event) -> None:
        self.events += 1
        if isinstance(event, TradePrint):
            self.now = max(self.now, event.ts) if self.events > 1 else event.ts
            self.last_prices[event.token_id] = event.price
            self._apply_fills(self.executor.on_trade_print(event))
        elif isinstance(event, BookEvent):
            self.now = event.ts
            self._apply_fills(self.executor.on_book(event.book))
        elif isinstance(event, PriceChangeEvent):
            self.now = event.ts
            book = self.executor.books.get(event.token_id)
            if book is not None:
                book.apply_price_change(event.change)
                self._apply_fills(self.executor.on_book(book))
        elif isinstance(event, PriceEvent):
            self.now = event.ts
            if self.fair is not None:
                if event.source == "binance":
                    self.fair.on_spot(event.ts, event.value)
                elif event.source == "chainlink.twap":
                    self.fair.on_reference(event.ts, event.value)
        elif isinstance(event, GapEvent):
            self.now = event.ts
            self.journal.record("gap", reason=event.reason)
            cancelled = self.executor.cancel_all("gap in market data")
            for o in cancelled:
                self.journal.record("order_cancelled", order_id=o.id, reason=o.reason)
            self.executor.books.clear()  # book unknown until the next snapshot

    def _lifecycle(self) -> None:
        for o in self.executor.expire(self.now):
            self.journal.record("order_expired", order_id=o.id, reason=o.reason)
        reason = self.risk.evaluate(self.marks(), self.now)
        if reason:
            self.halted = reason
            self._mark(force=True)  # the trough that tripped the breaker is on record
            self.journal.record("risk_halt", reason=reason)
            for o in self.executor.cancel_all("risk halt"):
                self.journal.record("order_cancelled", order_id=o.id, reason=o.reason)

    def _strategy(self) -> None:
        if self.risk.mode is not Mode.NORMAL:
            return
        p_up = self.fair.p_up(self.now) if self.fair is not None else None
        ctx = Context(
            market=self.market,
            now=self.now,
            books=self.executor.books,
            last_prices=self.last_prices,
            positions={t: p.size for t, p in self.portfolio.positions.items()},
            open_orders=self.executor.open_orders(),
            fair_up=Decimal(str(round(p_up, 6))) if p_up is not None else None,
        )
        marks = self.marks()
        for action in self.strategy.on_tick(ctx):
            if isinstance(action, Cancel):
                o = self.executor.cancel(action.order_id, action.reason)
                self.journal.record("order_cancelled", order_id=o.id, reason=o.reason)
                continue
            if isinstance(action, Take):
                self._take(action, marks)
                continue
            self._quote(action, marks)

    def _quote(self, q: Quote, marks: dict[str, Decimal]) -> None:
        intent = Intent(self.market.condition_id, q.token_id, q.side, q.price, q.size)
        decision = self.risk.check(intent, marks, self.executor.open_orders())
        if not decision.allowed:
            self.journal.record(
                "risk_block",
                token=q.token_id,
                side=q.side,
                price=q.price,
                size=q.size,
                reason=decision.reason,
            )
            return
        order, fills = self.executor.submit(
            q.token_id,
            q.side,  # type: ignore[arg-type]
            q.price,
            q.size,
            order_type=OrderType.GTD if q.ttl else OrderType.GTC,
            post_only=True,
            expires_at=(self.now + q.ttl) if q.ttl else None,
        )
        self.journal.record(
            "order_submitted" if order.status.value != "rejected" else "order_rejected",
            order_id=order.id,
            token=q.token_id,
            side=q.side,
            price=q.price,
            size=q.size,
            lean=q.lean,
            status=order.status,
            reason=order.reason,
        )
        self._apply_fills(fills)

    def _take(self, t: Take, marks: dict[str, Decimal]) -> None:
        intent = Intent(self.market.condition_id, t.token_id, "BUY", t.limit, t.size)
        decision = self.risk.check(intent, marks, self.executor.open_orders())
        if not decision.allowed:
            self.journal.record(
                "risk_block",
                token=t.token_id,
                side="BUY",
                price=t.limit,
                size=t.size,
                reason=decision.reason,
            )
            return
        order, fills = self.executor.submit(
            t.token_id, "BUY", t.limit, t.size, order_type=OrderType.FAK, post_only=False
        )
        self.journal.record(
            "order_submitted" if order.status.value != "rejected" else "order_rejected",
            order_id=order.id,
            token=t.token_id,
            side="BUY",
            price=t.limit,
            size=t.size,
            lean=t.lean,
            status=order.status,
            reason=order.reason,
            taker=True,
        )
        self._apply_fills(fills)

    # -- entry point --------------------------------------------------------------

    def run(
        self, events: Iterable[Event], *, payouts: dict[str, Decimal] | None = None
    ) -> RunResult:
        for event in events:
            self._handle(event)
            self._lifecycle()
            self._strategy()
            self._mark()
        for o in self.executor.cancel_all("run end"):
            self.journal.record("order_cancelled", order_id=o.id, reason=o.reason)
        settled = False
        if payouts:
            received = self.portfolio.settle(payouts)
            settled = True
            self.journal.record("settle", payouts=payouts, received=received)
        self._mark(force=True)
        if self.fair is not None:
            self.journal.record(
                "fair_value",
                start_price=self.fair.start_price,
                start_source=self.fair.start_source,
                last_reference=self.fair.reference_price,
                sigma_per_second=self.fair.sigma,
            )
        self.journal.record(
            "run_end",
            events=self.events,
            fills=len(self.fills),
            cash=self.portfolio.cash,
            realized_pnl=self.portfolio.realized_pnl,
            fees=self.portfolio.fees_paid,
            rebates_estimated=self.portfolio.rebates_estimated,
            halted=self.halted,
            settled=settled,
        )
        return RunResult(self.portfolio, self.fills, self.events, self.halted, settled, payouts)
