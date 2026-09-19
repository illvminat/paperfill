"""Pair detector: moments when buying both outcomes costs less than their guaranteed payout.

On a binary market one Up share plus one Down share pays exactly 1 at resolution. If the
best ask of Up plus the best ask of Down is below 1 by more than the taker fees on both
legs, a buyer of the pair locks in the difference. This is the within-venue half of what
cross-venue arbitrage bots look for; the cross-venue half needs a second venue's data
that the client must be licensed for (docs/decisions/0004-kalshi.md, `venues.py`).

Fees follow the market's schedule for each leg (`fees.py`); the pair size is the smaller
of the two best-ask sizes, so the "profit" is what one taker sweep of the top levels
would have captured, before anyone else did. It is an upper bound, not an expectation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfill.book import OrderBook
from paperfill.fees import FeeSchedule
from paperfill.markets import MarketInfo
from paperfill.runner import BookEvent, PriceChangeEvent, events_from_recording

ZERO, ONE = Decimal("0"), Decimal("1")
Q = Decimal("0.00001")


@dataclass(frozen=True, slots=True)
class PairQuote:
    ts: datetime
    ask_up: Decimal
    ask_down: Decimal
    size: Decimal  # min of the two best-ask sizes
    fee_per_pair: Decimal  # taker fee for one Up share plus one Down share at those prices
    net_per_pair: Decimal  # 1 - ask_up - ask_down - fee_per_pair

    @property
    def gross_per_pair(self) -> Decimal:
        return ONE - self.ask_up - self.ask_down


@dataclass(slots=True)
class PairScan:
    market: str
    question: str
    samples: int = 0  # book states examined
    profitable: int = 0  # states with net_per_pair > 0
    seconds_profitable: Decimal = ZERO  # time the book stayed profitable
    best: PairQuote | None = None
    total_net: Decimal = ZERO  # sum over profitable states of net_per_pair * size at first sight
    episodes: int = 0  # contiguous profitable stretches
    fee_rate: Decimal = ZERO
    min_gross_seen: Decimal | None = None  # smallest pair cost gap seen (may be negative)
    history: list[PairQuote] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "question": self.question,
            "samples": self.samples,
            "profitable_states": self.profitable,
            "episodes": self.episodes,
            "seconds_profitable": str(self.seconds_profitable.quantize(Decimal("0.001"))),
            "fee_rate": str(self.fee_rate),
            "best_net_per_pair": str(self.best.net_per_pair) if self.best else None,
            "best_size": str(self.best.size) if self.best else None,
            "best_at": self.best.ts.isoformat() if self.best else None,
            "total_net_first_sight": str(self.total_net.quantize(Q)),
            "min_gross_seen": str(self.min_gross_seen) if self.min_gross_seen is not None else None,
        }


def pair_quote(up: OrderBook, down: OrderBook, fees: FeeSchedule, ts: datetime) -> PairQuote | None:
    a, b = up.best_ask, down.best_ask
    if a is None or b is None:
        return None
    fee = fees.taker_fee(ONE, a.price) + fees.taker_fee(ONE, b.price)
    return PairQuote(
        ts=ts,
        ask_up=a.price,
        ask_down=b.price,
        size=min(a.size, b.size),
        fee_per_pair=fee,
        net_per_pair=ONE - a.price - b.price - fee,
    )


def scan_recording(path: Path, market: MarketInfo, *, keep_history: bool = False) -> PairScan:
    """Walk a recording and score every book state of the Up/Down pair."""
    scan = PairScan(market=market.condition_id, question=market.question, fee_rate=market.fees.rate)
    books: dict[str, OrderBook] = {}
    in_episode = False
    episode_started: datetime | None = None
    for e in events_from_recording(path):
        if isinstance(e, BookEvent):
            books[e.book.token_id] = e.book
        elif isinstance(e, PriceChangeEvent):
            b = books.get(e.token_id)
            if b is None:
                continue
            b.apply_price_change(e.change)
        else:
            continue
        up, down = books.get(market.yes_token), books.get(market.no_token)
        if up is None or down is None:
            continue
        q = pair_quote(up, down, market.fees, e.ts)
        if q is None:
            continue
        scan.samples += 1
        gross = q.gross_per_pair
        scan.min_gross_seen = (
            gross if scan.min_gross_seen is None else min(scan.min_gross_seen, gross)
        )
        if keep_history:
            scan.history.append(q)
        if q.net_per_pair > ZERO:
            scan.profitable += 1
            if scan.best is None or q.net_per_pair > scan.best.net_per_pair:
                scan.best = q
            if not in_episode:
                in_episode, episode_started = True, e.ts
                scan.episodes += 1
                scan.total_net += q.net_per_pair * q.size
        elif in_episode:
            assert episode_started is not None
            scan.seconds_profitable += Decimal(str((e.ts - episode_started).total_seconds()))
            in_episode = False
    return scan


def summarize(scans: list[PairScan]) -> dict[str, Any]:
    with_pairs = [s for s in scans if s.profitable]
    return {
        "windows": len(scans),
        "windows_with_profitable_pair": len(with_pairs),
        "episodes": sum(s.episodes for s in scans),
        "seconds_profitable_total": str(
            sum((s.seconds_profitable for s in scans), ZERO).quantize(Decimal("0.001"))
        ),
        "best_net_per_pair": str(
            max((s.best.net_per_pair for s in with_pairs if s.best is not None), default=ZERO)
        ),
        "total_net_first_sight": str(sum((s.total_net for s in scans), ZERO).quantize(Q)),
    }


def to_markdown(scans: list[PairScan], summary: dict[str, Any]) -> str:
    lines = [
        "# paperfill pair scan",
        "",
        "Moments when best ask(Up) + best ask(Down) < 1 minus taker fees on both legs.",
        "",
        "| Window | Book states | Profitable | Episodes | Seconds "
        "| Best net/pair | Best size | Net at first sight |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scans:
        secs = s.seconds_profitable.quantize(Decimal("0.1"))
        best_net = s.best.net_per_pair if s.best else "—"
        best_size = s.best.size if s.best else "—"
        lines.append(
            f"| {s.question} | {s.samples} | {s.profitable} | {s.episodes} | {secs} "
            f"| {best_net} | {best_size} | {s.total_net.quantize(Q)} |"
        )
    lines += ["", "| Summary | |", "|---|---|"]
    for k, v in summary.items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "Net at first sight = net per pair x available size when an episode begins: what one",
        "taker sweep would have captured before anyone else. An upper bound, not an expectation:",
        "the recording shows the book after the fact, and the fastest bots see it first.",
        "",
    ]
    return "\n".join(lines)
