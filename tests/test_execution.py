"""Fill model against hand-computed references (numbers worked out in the comments)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from paperfill.book import OrderBook
from paperfill.execution import (
    Fill,
    MarketParams,
    OrderStatus,
    OrderType,
    PaperExecutor,
    Portfolio,
)
from paperfill.fees import FeeSchedule
from paperfill.history import TradePrint

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
UP = "1" * 10


def crypto_params() -> MarketParams:
    fees = FeeSchedule.from_gamma(
        {"rate": "0.07", "exponent": 1, "takerOnly": True, "rebateRate": "0.2"}, fees_enabled=True
    )
    return MarketParams(fees=fees, tick_size=D("0.01"), min_order_size=D("5"))


def book(bids, asks, token=UP) -> OrderBook:
    return OrderBook.from_snapshot(
        {
            "token_id": token,
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
        }
    )


def executor(clock_start=T0) -> PaperExecutor:
    t = [clock_start]

    def clock():
        t[0] += timedelta(seconds=1)
        return t[0]

    return PaperExecutor(crypto_params(), clock=clock)


def test_taker_walks_levels_and_pays_fee_per_level():
    ex = executor()
    ex.on_book(book(bids=[("0.58", "100")], asks=[("0.60", "10"), ("0.61", "5"), ("0.62", "20")]))
    order, fills = ex.submit(UP, "BUY", D("0.62"), D("12"))
    # 10 @ 0.60 then 2 @ 0.61. Fees (rate 0.07, p(1-p)):
    #   10 * 0.07 * 0.60 * 0.40 = 0.168
    #    2 * 0.07 * 0.61 * 0.39 = 0.033306 -> 0.03331
    assert [(f.price, f.size, f.fee, f.liquidity) for f in fills] == [
        (D("0.60"), D("10"), D("0.16800"), "taker"),
        (D("0.61"), D("2"), D("0.03331"), "taker"),
    ]
    assert order.status is OrderStatus.FILLED and order.remaining == 0
    assert ex.books[UP].asks == {D("0.61"): D("3"), D("0.62"): D("20")}  # depth consumed


def test_partial_taker_then_rests_remainder_as_gtc():
    ex = executor()
    ex.on_book(book(bids=[], asks=[("0.60", "10")]))
    order, fills = ex.submit(UP, "BUY", D("0.60"), D("15"))
    assert len(fills) == 1 and fills[0].size == D("10")
    assert order.status is OrderStatus.OPEN and order.remaining == D("5")
    assert ex.open_orders(UP) == [order]


def test_fok_rejected_without_full_depth_and_fak_cancels_remainder():
    ex = executor()
    ex.on_book(book(bids=[], asks=[("0.60", "10")]))
    fok, fills = ex.submit(UP, "BUY", D("0.60"), D("15"), order_type=OrderType.FOK)
    assert fok.status is OrderStatus.REJECTED and fills == [] and "FOK" in fok.reason
    assert ex.books[UP].asks == {D("0.60"): D("10")}  # nothing consumed
    fak, fills = ex.submit(UP, "BUY", D("0.60"), D("15"), order_type=OrderType.FAK)
    assert fills[0].size == D("10") and fak.status is OrderStatus.CANCELLED
    assert ex.open_orders(UP) == []


def test_post_only_that_would_cross_is_rejected():
    ex = executor()
    ex.on_book(book(bids=[], asks=[("0.60", "10")]))
    order, fills = ex.submit(UP, "BUY", D("0.60"), D("5"), post_only=True)
    assert order.status is OrderStatus.REJECTED and fills == []
    resting, _ = ex.submit(UP, "BUY", D("0.59"), D("5"), post_only=True)
    assert resting.status is OrderStatus.OPEN


@pytest.mark.parametrize(
    ("price", "size", "fragment"),
    [
        ("0.605", "5", "tick"),
        ("0.60", "4", "minimum order size"),
        ("0", "5", "outside"),
        ("1", "5", "outside"),
    ],
)
def test_validation_rejects_bad_price_or_size(price, size, fragment):
    ex = executor()
    order, fills = ex.submit(UP, "BUY", D(price), D(size))
    assert order.status is OrderStatus.REJECTED and fragment in order.reason and fills == []


def test_tick_and_min_size_follow_the_book():
    ex = executor()
    snap = book(bids=[], asks=[])
    snap.tick_size, snap.min_order_size = D("0.001"), D("1")
    ex.on_book(snap)
    order, _ = ex.submit(UP, "BUY", D("0.605"), D("1"))
    assert order.status is OrderStatus.OPEN


def test_resting_bid_fills_as_maker_when_print_goes_through_its_price():
    ex = executor()
    ex.on_book(book(bids=[("0.57", "50")], asks=[("0.60", "10")]))
    order, _ = ex.submit(UP, "BUY", D("0.58"), D("5"))
    at_price = TradePrint(T0, 0, "SELL", D("0.58"), D("3"), UP, "Up")
    assert ex.on_trade_print(at_price) == []  # exactly at our price: queue unknown, no fill
    through = TradePrint(T0, 1, "SELL", D("0.57"), D("3"), UP, "Up")
    fills = ex.on_trade_print(through)
    # maker: no fee; rebate estimate = 0.2 * fee-equivalent, where
    #   3 * 0.07 * 0.58 * 0.42 = 0.051156 -> 0.05116; 0.2 * 0.05116 = 0.010232 -> 0.01023
    assert fills == [
        Fill(order.id, UP, "BUY", D("0.58"), D("3"), D("0"), D("0.01023"), "maker", T0)
    ]
    assert order.remaining == D("2") and order.status is OrderStatus.OPEN


def test_resting_ask_fills_once_per_crossing_of_the_book():
    ex = executor()
    ex.on_book(book(bids=[("0.55", "10")], asks=[("0.60", "10")]))
    order, _ = ex.submit(UP, "SELL", D("0.59"), D("5"))
    assert ex.on_book(book(bids=[("0.59", "10")], asks=[("0.61", "10")])) == []  # touch only
    fills = ex.on_book(book(bids=[("0.60", "2")], asks=[("0.61", "10")]))  # bid through 0.59
    assert [(f.price, f.size, f.liquidity) for f in fills] == [(D("0.59"), D("2"), "maker")]
    assert order.remaining == D("3")
    # the recording keeps showing the crossing level: no second fill from the same crossing
    assert ex.on_book(book(bids=[("0.60", "2")], asks=[("0.61", "10")])) == []
    assert ex.on_book(book(bids=[("0.62", "9")], asks=[("0.63", "10")])) == []
    # un-cross, then cross again -> one more fill
    assert ex.on_book(book(bids=[("0.58", "9")], asks=[("0.63", "10")])) == []
    fills = ex.on_book(book(bids=[("0.61", "1")], asks=[("0.63", "10")]))
    assert [(f.size) for f in fills] == [D("1")] and order.remaining == D("2")


def test_gtd_expires_and_cancel_all():
    ex = executor()
    gtd, _ = ex.submit(
        UP, "BUY", D("0.50"), D("5"), order_type=OrderType.GTD, expires_at=T0 + timedelta(minutes=1)
    )
    gtc, _ = ex.submit(UP, "BUY", D("0.49"), D("5"))
    assert ex.expire(T0 + timedelta(seconds=30)) == []
    assert ex.expire(T0 + timedelta(minutes=1)) == [gtd] and gtd.status is OrderStatus.EXPIRED
    assert ex.cancel_all() == [gtc] and gtc.status is OrderStatus.CANCELLED
    bad, _ = ex.submit(UP, "BUY", D("0.50"), D("5"), order_type=OrderType.GTD)
    assert bad.status is OrderStatus.REJECTED


def test_portfolio_accounting_and_settlement():
    pf = Portfolio(cash=D("100"))
    buy = Fill("o1", UP, "BUY", D("0.60"), D("10"), D("0.168"), D("0"), "taker", T0)
    pf.apply(buy)
    # cost basis includes the buy fee: 6.00 + 0.168 = 6.168, average 0.6168
    assert pf.cash == D("93.832000") and pf.positions[UP].size == D("10")
    assert pf.positions[UP].average_price == D("0.6168")
    sell = Fill("o2", UP, "SELL", D("0.70"), D("4"), D("0.0588"), D("0"), "taker", T0)
    pf.apply(sell)
    # realized: 4 * 0.70 - 4 * 0.6168 - 0.0588 = 2.80 - 2.4672 - 0.0588 = 0.274
    assert pf.realized_pnl == D("0.274") and pf.positions[UP].size == D("6")
    assert pf.cash == D("93.832000") + D("2.8") - D("0.0588")
    with pytest.raises(ValueError):
        pf.apply(Fill("o3", UP, "SELL", D("0.70"), D("7"), D("0"), D("0"), "taker", T0))
    received = pf.settle({UP: D("1")})
    # remaining 6 shares carry basis 3.7008, pay 6.00 -> +2.2992 realized
    assert received == D("6") and pf.realized_pnl == D("0.274") + D("2.2992")
    # and the whole story reconciles with cash: 100 -> 102.5732 = +2.5732 = total realized
    assert pf.cash - D("100") == pf.realized_pnl
    assert pf.positions[UP].size == 0 and pf.exposure({UP: D("0.5")}) == 0
    assert pf.equity({}) == pf.cash
    assert pf.fees_paid == D("0.2268")
