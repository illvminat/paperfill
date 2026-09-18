"""Order book for one outcome token, rebuilt from snapshots and incremental changes.

Source of truth: SOURCES.md -> polymarket/prices-order-books.md (REST `/book`),
polymarket/realtime-data.md (`book` snapshot and `price_change` events).

Level semantics of `price_change`: the event carries `price`, `size` and `side` for a
token. The documentation does not state whether `size` is the new resting size at that
price or a delta. `apply_price_change` treats it as the *new resting size* (0 removes
the level). This reading was verified against a live recording where every incoming
`book` snapshot matched the book rebuilt from the preceding changes
(docs/measurements/2026-09-19-price-change-semantics.md).

Prices are `Decimal`; the best bid is the highest bid price, the best ask the lowest
ask price. Both REST and WebSocket order levels by price with the best level last
(docs/measurements/2026-09-19-api-probe.md); this class does not rely on that order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(slots=True)
class OrderBook:
    token_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    ts: datetime | None = None
    hash: str | None = None

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any]) -> OrderBook:
        """Build from a REST `/book` response or a `book` stream payload (same fields)."""
        token = str(payload.get("token_id") or payload.get("asset_id"))
        book = cls(token_id=token)
        for lvl in payload.get("bids", []):
            size = Decimal(str(lvl["size"]))
            if size > ZERO:
                book.bids[Decimal(str(lvl["price"]))] = size
        for lvl in payload.get("asks", []):
            size = Decimal(str(lvl["size"]))
            if size > ZERO:
                book.asks[Decimal(str(lvl["price"]))] = size
        if payload.get("tick_size") is not None:
            book.tick_size = Decimal(str(payload["tick_size"]))
        if payload.get("min_order_size") is not None:
            book.min_order_size = Decimal(str(payload["min_order_size"]))
        ts = payload.get("timestamp")
        if isinstance(ts, datetime):
            book.ts = ts
        elif isinstance(ts, str) and not ts.isdigit():
            book.ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        book.hash = payload.get("hash")
        return book

    def apply_price_change(self, change: dict[str, Any]) -> None:
        """Set the resting size at `price` on `side`; a zero size removes the level."""
        side = self.bids if change["side"] == "BUY" else self.asks
        price, size = Decimal(str(change["price"])), Decimal(str(change["size"]))
        if size <= ZERO:
            side.pop(price, None)
        else:
            side[price] = size

    @property
    def best_bid(self) -> Level | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return Level(p, self.bids[p])

    @property
    def best_ask(self) -> Level | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return Level(p, self.asks[p])

    @property
    def mid(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (b.price + a.price) / 2

    @property
    def spread(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return a.price - b.price

    def asks_ascending(self) -> list[Level]:
        return [Level(p, self.asks[p]) for p in sorted(self.asks)]

    def bids_descending(self) -> list[Level]:
        return [Level(p, self.bids[p]) for p in sorted(self.bids, reverse=True)]

    def snapshot_levels(self) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        """Canonical, comparable form: ((price, size), ...) for bids then asks."""
        return (
            tuple((str(p), str(s)) for p, s in sorted(self.bids.items())),
            tuple((str(p), str(s)) for p, s in sorted(self.asks.items())),
        )
