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
