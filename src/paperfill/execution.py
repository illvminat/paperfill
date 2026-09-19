"""Paper execution: orders, fills, fees and a portfolio, against a live or replayed book.

Sources of truth: SOURCES.md -> polymarket/place-orders.md (order types GTC, GTD, FOK,
FAK, post-only), polymarket/prices-order-books.md (tick size, minimum order size),
polymarket/fees.md and market-details.md (fees). The fill *model* below is ours and is
deliberately conservative; it is not a claim about the matching engine:

- An order that crosses the book is filled level by level as a taker and pays the
  taker fee on every fill.
- A resting order is filled as a maker (no fee, an estimated rebate) only when the
  market trades *through* its price: in replay, a print strictly better than the
  order price; in live recording, the opposite side of the book crossing the order
  price. Trades exactly at the order price are not counted as fills, because queue
  position is unknown.

There is exactly one executor and it never sends anything anywhere.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from paperfill.book import OrderBook
from paperfill.fees import FeeSchedule
from paperfill.history import TradePrint

Side = Literal["BUY", "SELL"]
ZERO, ONE = Decimal("0"), Decimal("1")
CASH_PRECISION = Decimal("0.000001")
"""USDC has six decimals (docs/zadanie.md, section 6)."""


class OrderType(StrEnum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class OrderStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(slots=True)
class Order:
    id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    post_only: bool = False
    expires_at: datetime | None = None
    created_at: datetime | None = None
    remaining: Decimal = field(default=ZERO)
    status: OrderStatus = OrderStatus.OPEN
    reason: str | None = None
    crossed: bool = False  # the book currently sits through this order's price

    def __post_init__(self) -> None:
        self.remaining = self.size


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    rebate_estimate: Decimal
    liquidity: Literal["maker", "taker"]
    ts: datetime

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(slots=True)
class MarketParams:
    fees: FeeSchedule
    tick_size: Decimal
    min_order_size: Decimal


class PaperExecutor:
    def __init__(
        self,
        params: MarketParams,
        *,
        clock: Callable[[], datetime],
        ids: Iterable[str] | None = None,
    ) -> None:
        self.params = params
        self.clock = clock
        self._ids = iter(ids) if ids is not None else (f"o{n}" for n in itertools.count(1))
        self.books: dict[str, OrderBook] = {}
        self.orders: dict[str, Order] = {}

    # -- market data ---------------------------------------------------------------

    def on_book(self, book: OrderBook) -> list[Fill]:
        """Replace the book for a token; resting orders crossed by it are filled."""
        self.books[book.token_id] = book
        if book.tick_size is not None:
            self.params.tick_size = book.tick_size
        if book.min_order_size is not None:
            self.params.min_order_size = book.min_order_size
        return self._match_resting_against_book(book.token_id)

    def on_trade_print(self, print_: TradePrint) -> list[Fill]:
        """Replay mode: a print through a resting order's price fills it (maker)."""
        fills: list[Fill] = []
        available = print_.size
        for order in self._open_orders(print_.token_id):
            if available <= ZERO:
                break
            through = (
                print_.price < order.price if order.side == "BUY" else print_.price > order.price
            )
            if not through:
                continue
            size = min(order.remaining, available)
            available -= size
            fills.append(self._fill(order, order.price, size, "maker", print_.ts))
        return fills

    def expire(self, now: datetime | None = None) -> list[Order]:
        now = now or self.clock()
        expired = []
        for order in list(self.orders.values()):
            if (
                order.status is OrderStatus.OPEN
                and order.expires_at is not None
                and order.expires_at <= now
            ):
                order.status, order.reason = OrderStatus.EXPIRED, "GTD expiry"
                expired.append(order)
        return expired

    # -- orders --------------------------------------------------------------------

    def submit(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        *,
        order_type: OrderType = OrderType.GTC,
        post_only: bool = False,
        expires_at: datetime | None = None,
    ) -> tuple[Order, list[Fill]]:
        now = self.clock()
        order = Order(
            id=next(self._ids),
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            post_only=post_only,
            expires_at=expires_at,
            created_at=now,
        )
        self.orders[order.id] = order
        reason = self._validate(order)
        if reason:
            order.status, order.reason, order.remaining = OrderStatus.REJECTED, reason, ZERO
            return order, []
        book = self.books.get(token_id)
        crossing = self._crossing_levels(order, book) if book is not None else []
        if order.post_only and crossing:
            order.status, order.reason, order.remaining = (
                OrderStatus.REJECTED,
                "post-only order would cross the book",
                ZERO,
            )
            return order, []
        if order.order_type is OrderType.FOK and sum(s for _, s in crossing) < order.size:
            order.status, order.reason, order.remaining = (
                OrderStatus.REJECTED,
                "FOK: insufficient depth for full fill",
                ZERO,
            )
            return order, []
        fills: list[Fill] = []
        for level_price, level_size in crossing:
            if order.remaining <= ZERO:
                break
            size = min(order.remaining, level_size)
            fills.append(self._fill(order, level_price, size, "taker", now))
            assert book is not None  # crossing levels only exist with a book
            self._consume(book, order.side, level_price, size)
        if order.remaining > ZERO and order.order_type in (OrderType.FOK, OrderType.FAK):
            order.status, order.reason, order.remaining = (
                OrderStatus.CANCELLED,
                "FAK: remainder cancelled",
                ZERO,
            )
        return order, fills

    def cancel(self, order_id: str, reason: str = "cancelled by strategy") -> Order:
        order = self.orders[order_id]
        if order.status is OrderStatus.OPEN:
            order.status, order.reason, order.remaining = OrderStatus.CANCELLED, reason, ZERO
        return order

    def cancel_all(self, reason: str = "cancel all") -> list[Order]:
        return [
            self.cancel(o.id, reason)
            for o in list(self.orders.values())
            if o.status is OrderStatus.OPEN
        ]

    def open_orders(self, token_id: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.status is OrderStatus.OPEN and (token_id is None or o.token_id == token_id)
        ]

    # -- internals -----------------------------------------------------------------

    def _open_orders(self, token_id: str) -> list[Order]:
        return sorted(self.open_orders(token_id), key=lambda o: o.created_at or self.clock())

    def _validate(self, order: Order) -> str | None:
        p, tick, min_size = order.price, self.params.tick_size, self.params.min_order_size
        if not ZERO < p < ONE:
            return f"price {p} outside (0, 1)"
        if (p / tick) % 1 != 0:
            return f"price {p} not a multiple of tick {tick}"
        if order.size < min_size:
            return f"size {order.size} below minimum order size {min_size}"
        if order.order_type is OrderType.GTD and order.expires_at is None:
            return "GTD order needs expires_at"
        if order.post_only and order.order_type in (OrderType.FOK, OrderType.FAK):
            return "post-only cannot be combined with FOK/FAK"
        return None

    @staticmethod
    def _crossing_levels(order: Order, book: OrderBook) -> list[tuple[Decimal, Decimal]]:
        if order.side == "BUY":
            return [(lv.price, lv.size) for lv in book.asks_ascending() if lv.price <= order.price]
        return [(lv.price, lv.size) for lv in book.bids_descending() if lv.price >= order.price]

    @staticmethod
    def _consume(book: OrderBook, side: Side, price: Decimal, size: Decimal) -> None:
        levels = book.asks if side == "BUY" else book.bids
        left = levels[price] - size
        if left <= ZERO:
            del levels[price]
        else:
            levels[price] = left

    def _match_resting_against_book(self, token_id: str) -> list[Fill]:
        """Fill resting orders when the opposite side *starts* sitting through their price.

        A recording keeps showing the crossing level (we were never really there), so a
        fill is taken once per crossing: on the transition from "not crossed" to
        "crossed". The order is re-armed only after the book un-crosses.
        """
        book = self.books[token_id]
        fills: list[Fill] = []
        for order in self._open_orders(token_id):
            opposite = book.best_ask if order.side == "BUY" else book.best_bid
            if opposite is None:
                order.crossed = False
                continue
            crossed = (
                opposite.price < order.price
                if order.side == "BUY"
                else opposite.price > order.price
            )
            if not crossed:
                order.crossed = False
                continue
            if order.crossed:
                continue
            order.crossed = True
            size = min(order.remaining, opposite.size)
            fills.append(self._fill(order, order.price, size, "maker", book.ts or self.clock()))
        return fills

    def _fill(
        self,
        order: Order,
        price: Decimal,
        size: Decimal,
        liquidity: Literal["maker", "taker"],
        ts: datetime,
    ) -> Fill:
        fee = self.params.fees.taker_fee(size, price) if liquidity == "taker" else ZERO
        rebate = (
            self.params.fees.maker_rebate_estimate(size, price) if liquidity == "maker" else ZERO
        )
        order.remaining -= size
        if order.remaining <= ZERO:
            order.status, order.remaining = OrderStatus.FILLED, ZERO
        return Fill(
            order_id=order.id,
            token_id=order.token_id,
            side=order.side,
            price=price,
            size=size,
            fee=fee,
            rebate_estimate=rebate,
            liquidity=liquidity,
            ts=ts,
        )


@dataclass(slots=True)
class Position:
    size: Decimal = ZERO
    cost: Decimal = ZERO  # cash paid for the current size including buy fees (cost basis)

    @property
    def average_price(self) -> Decimal | None:
        return (self.cost / self.size) if self.size else None


@dataclass(slots=True)
class Portfolio:
    """Cash and outcome-token positions; long-only, as on the CLOB (no shorting)."""

    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    fees_paid: Decimal = ZERO
    rebates_estimated: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    def apply(self, fill: Fill) -> None:
        pos = self.positions.setdefault(fill.token_id, Position())
        self.fees_paid += fill.fee
        self.rebates_estimated += fill.rebate_estimate
        if fill.side == "BUY":
            # the fee is part of what the shares cost: it stays in the basis until sold/settled
            self.cash -= fill.notional + fill.fee
            pos.size += fill.size
            pos.cost += fill.notional + fill.fee
        else:
            if fill.size > pos.size:
                raise ValueError(f"cannot sell {fill.size} of {fill.token_id}: holding {pos.size}")
            avg = pos.cost / pos.size
            self.cash += fill.notional - fill.fee
            self.realized_pnl += fill.notional - avg * fill.size - fill.fee
            pos.cost -= avg * fill.size
            pos.size -= fill.size
        self.cash = self.cash.quantize(CASH_PRECISION, rounding=ROUND_HALF_UP)

    def settle(self, payouts: dict[str, Decimal]) -> Decimal:
        """Resolve tokens at their payout (1 or 0 per share); returns cash received."""
        received = ZERO
        for token, payout in payouts.items():
            pos = self.positions.get(token)
            if not pos or pos.size == ZERO:
                continue
            value = (pos.size * payout).quantize(CASH_PRECISION, rounding=ROUND_HALF_UP)
            self.realized_pnl += value - pos.cost
            self.cash += value
            received += value
            pos.size, pos.cost = ZERO, ZERO
        return received

    def exposure(self, marks: dict[str, Decimal]) -> Decimal:
        """Current value of open positions at the given marks (mid prices)."""
        return sum((pos.size * marks.get(t, ZERO) for t, pos in self.positions.items()), ZERO)

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + self.exposure(marks)
