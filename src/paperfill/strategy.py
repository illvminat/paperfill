"""Strategy interface and one reference strategy.

The reference strategy is a *sample* showing how a strategy plugs in. It is not
advice and it is not claimed to be profitable; the report will say what it did.

Reference: two-sided inventory quoting on a short Up/Down market. Each cycle it bids
on both outcome tokens at (or just below) the best bid, so that a full pair costs at
most one dollar, and sizes the two bids asymmetrically by a lean derived from the
recent drift of the Up mid. Positions are held to resolution.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from paperfill.book import OrderBook
from paperfill.execution import Order
from paperfill.markets import MarketInfo

ZERO, ONE = Decimal("0"), Decimal("1")


@dataclass(frozen=True, slots=True)
class Quote:
    token_id: str
    side: str
    price: Decimal
    size: Decimal
    ttl: timedelta | None
    lean: Decimal  # signed conviction attached to the quote, for the report's buckets


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: str
    reason: str


Action = Quote | Cancel


@dataclass(slots=True)
class Context:
    """What a strategy may look at; everything else is off limits by construction."""

    market: MarketInfo
    now: datetime
    books: dict[str, OrderBook]
    last_prices: dict[str, Decimal]
    positions: dict[str, Decimal]  # shares held per token
    open_orders: list[Order]
    fair_up: Decimal | None = None  # external fair probability of "Up", if a feed is present

    def reference_price(self, token_id: str) -> Decimal | None:
        """Mid if a two-sided book exists, else the last print; None if nothing known."""
        book = self.books.get(token_id)
        if book is not None and book.mid is not None:
            return book.mid
        return self.last_prices.get(token_id)

    def best_bid(self, token_id: str) -> Decimal | None:
        book = self.books.get(token_id)
        if book is not None and book.best_bid is not None:
            return book.best_bid.price
        return None


class Strategy(Protocol):
    name: str

    def on_tick(self, ctx: Context) -> list[Action]: ...


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


@dataclass(slots=True)
class TwoSidedQuoter:
    """Bid both tokens; lean sizes with the drift of the Up mid over `lookback`."""

    size: Decimal = Decimal("5")
    lookback: timedelta = timedelta(seconds=30)
    lean_gain: Decimal = Decimal("20")  # lean = clamp(gain * drift, -max_lean, max_lean)
    max_lean: Decimal = Decimal("0.5")
    requote_every: timedelta = timedelta(seconds=5)
    max_inventory: Decimal = Decimal("50")  # shares per token
    quote_ttl: timedelta = timedelta(seconds=15)
    name: str = "two-sided-quoter"
    _history: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    _last_quote_at: datetime | None = None

    def lean(self, now: datetime, up_mid: Decimal) -> Decimal:
        self._history.append((now, up_mid))
        while self._history and now - self._history[0][0] > self.lookback:
            self._history.popleft()
        drift = up_mid - self._history[0][1]
        lean = self.lean_gain * drift
        return max(-self.max_lean, min(self.max_lean, lean))

    def on_tick(self, ctx: Context) -> list[Action]:
        up, down = ctx.market.yes_token, ctx.market.no_token
        up_ref = ctx.reference_price(up)
        if up_ref is None:
            return []
        lean = self.lean(ctx.now, up_ref)
        if self._last_quote_at is not None and ctx.now - self._last_quote_at < self.requote_every:
            return []
        self._last_quote_at = ctx.now
        actions: list[Action] = [Cancel(o.id, "requote") for o in ctx.open_orders]
        tick = ctx.market.tick_size
        for token, factor in ((up, ONE + lean), (down, ONE - lean)):
            if ctx.positions.get(token, ZERO) >= self.max_inventory:
                continue
            ref = ctx.reference_price(token)
            bid = ctx.best_bid(token)
            if ref is None:
                continue
            # join the best bid when a book exists; on a bare tape, one tick under the print
            price = bid if bid is not None else _round_to_tick(ref - tick, tick)
            price = _round_to_tick(price, tick)
            if not ZERO < price < ONE:
                continue
            size = (self.size * factor).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if size < ctx.market.min_order_size:
                continue
            actions.append(Quote(token, "BUY", price, size, self.quote_ttl, lean))
        return actions


@dataclass(slots=True)
class FairValueQuoter:
    """Bid both tokens at the best bid, sized by the edge of an external fair value.

    Edge on a token is `fair - reference price`. Both bids are placed only when the
    pair costs at most one dollar (`bid_up + bid_down <= 1`); otherwise only the side
    with positive edge above `min_edge` is quoted. Sizes scale with edge up to
    `max_ratio` times the base size. Without a fair value the strategy does nothing
    and says so through the runner's journal (no quotes, no guesses).
    """

    size: Decimal = Decimal("5")
    min_edge: Decimal = Decimal("0.02")
    edge_gain: Decimal = Decimal("10")  # size factor = clamp(1 + gain * edge, 0, max_ratio)
    max_ratio: Decimal = Decimal("3")
    requote_every: timedelta = timedelta(seconds=5)
    max_inventory: Decimal = Decimal("50")
    quote_ttl: timedelta | None = timedelta(seconds=15)
    name: str = "fair-value-quoter"
    _last_quote_at: datetime | None = None

    def on_tick(self, ctx: Context) -> list[Action]:
        if ctx.fair_up is None:
            return []
        if self._last_quote_at is not None and ctx.now - self._last_quote_at < self.requote_every:
            return []
        self._last_quote_at = ctx.now
        up, down = ctx.market.yes_token, ctx.market.no_token
        tick = ctx.market.tick_size
        fair = {up: ctx.fair_up, down: ONE - ctx.fair_up}
        actions: list[Action] = [Cancel(o.id, "requote") for o in ctx.open_orders]
        bids: dict[str, Decimal] = {}
        edges: dict[str, Decimal] = {}
        for token in (up, down):
            ref, bid = ctx.reference_price(token), ctx.best_bid(token)
            if ref is None:
                continue
            price = bid if bid is not None else _round_to_tick(ref - tick, tick)
            price = _round_to_tick(price, tick)
            if not ZERO < price < ONE:
                continue
            bids[token] = price
            edges[token] = fair[token] - ref
        if not bids:
            return actions
        pair_ok = len(bids) == 2 and sum(bids.values(), ZERO) <= ONE
        for token, price in bids.items():
            edge = edges[token]
            if not pair_ok and edge < self.min_edge:
                continue
            if ctx.positions.get(token, ZERO) >= self.max_inventory:
                continue
            factor = max(ZERO, min(self.max_ratio, ONE + self.edge_gain * edge))
            size = (self.size * factor).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if size < ctx.market.min_order_size:
                continue
            actions.append(Quote(token, "BUY", price, size, self.quote_ttl, edge))
        return actions
