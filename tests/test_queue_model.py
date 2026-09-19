"""Queue model: a resting order waits behind the size already at its price."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.book import OrderBook
from paperfill.execution import MarketParams, PaperExecutor
from paperfill.fees import FeeSchedule
from paperfill.history import TradePrint

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
UP = "1" * 10


def params(queue=True, ahead="0"):
    fees = FeeSchedule.from_gamma(
        {"rate": "0.07", "exponent": 1, "takerOnly": True, "rebateRate": "0.2"}, fees_enabled=True
    )
    return MarketParams(fees, D("0.01"), D("5"), queue_model=queue, assumed_queue_ahead=D(ahead))


def book(bids, asks):
    return OrderBook.from_snapshot(
        {"token_id": UP, "bids": [{"price": p, "size": s} for p, s in bids],
         "asks": [{"price": p, "size": s} for p, s in asks]}
    )  # fmt: skip


def executor(p=None):
    t = [T0]

    def clock():
        t[0] += timedelta(seconds=1)
        return t[0]

    return PaperExecutor(p or params(), clock=clock)


def test_joining_a_level_queues_behind_it_and_prints_at_price_consume_the_queue_first():
    ex = executor()
    ex.on_book(book(bids=[("0.60", "8")], asks=[("0.62", "10")]))
    order, _ = ex.submit(UP, "BUY", D("0.60"), D("5"))  # joins 8 already resting
    assert order.ahead == D("8")
    # a SELL print of 6 at 0.60: all six go to the queue ahead, none to us
    assert ex.on_trade_print(TradePrint(T0, 0, "SELL", D("0.60"), D("6"), UP, "Up")) == []
    assert order.ahead == D("2")
    # a SELL print of 5 at 0.60: 2 finish the queue, 3 reach us
    fills = ex.on_trade_print(TradePrint(T0, 1, "SELL", D("0.60"), D("5"), UP, "Up"))
    assert [(f.size, f.liquidity) for f in fills] == [(D("3"), "maker")]
    assert order.ahead == D("0") and order.remaining == D("2")
    # a print through the price clears the level: the rest fills
    fills = ex.on_trade_print(TradePrint(T0, 2, "SELL", D("0.59"), D("10"), UP, "Up"))
    assert fills[0].size == D("2") and order.remaining == D("0")


def test_without_queue_model_prints_at_price_never_fill():
    ex = executor(params(queue=False))
    ex.on_book(book(bids=[("0.60", "8")], asks=[("0.62", "10")]))
    order, _ = ex.submit(UP, "BUY", D("0.60"), D("5"))
    assert order.ahead == D("0")
    assert ex.on_trade_print(TradePrint(T0, 0, "SELL", D("0.60"), D("50"), UP, "Up")) == []
    assert ex.on_trade_print(TradePrint(T0, 1, "SELL", D("0.59"), D("1"), UP, "Up"))[0].size == D(
        "1"
    )


def test_level_shrink_caps_the_queue_and_book_crossing_shares_size_with_it():
    ex = executor()
    ex.on_book(book(bids=[("0.60", "8")], asks=[("0.62", "10")]))
    order, _ = ex.submit(UP, "BUY", D("0.60"), D("5"))
    # cancels ahead of us: the level drops to 3, so at most 3 can be ahead
    assert ex.on_book(book(bids=[("0.60", "3")], asks=[("0.62", "10")])) == []
    assert order.ahead == D("3")
    # the ask crosses through 0.60 with 4 shares: 3 go to the queue, 1 to us
    fills = ex.on_book(book(bids=[("0.60", "3")], asks=[("0.59", "4")]))
    assert [(f.price, f.size) for f in fills] == [(D("0.60"), D("1"))]
    assert order.ahead == D("0") and order.remaining == D("4")
    # a crossing smaller than the queue fills nothing
    ex2 = executor()
    ex2.on_book(book(bids=[("0.60", "8")], asks=[("0.62", "10")]))
    o2, _ = ex2.submit(UP, "BUY", D("0.60"), D("5"))
    assert ex2.on_book(book(bids=[("0.60", "8")], asks=[("0.59", "2")])) == []
    assert o2.ahead == D("6")


def test_tape_only_replay_uses_the_assumed_queue():
    ex = executor(params(ahead="10"))
    order, _ = ex.submit(UP, "BUY", D("0.60"), D("5"))  # no book known
    assert order.ahead == D("10")
    assert ex.on_trade_print(TradePrint(T0, 0, "SELL", D("0.60"), D("10"), UP, "Up")) == []
    assert ex.on_trade_print(TradePrint(T0, 1, "SELL", D("0.60"), D("2"), UP, "Up"))[0].size == D(
        "2"
    )


def test_new_price_level_has_no_queue():
    ex = executor()
    ex.on_book(book(bids=[("0.60", "8")], asks=[("0.62", "10")]))
    order, _ = ex.submit(UP, "BUY", D("0.61"), D("5"))  # a new best bid: first in line
    assert order.ahead == D("0")
    assert ex.on_trade_print(TradePrint(T0, 0, "SELL", D("0.61"), D("2"), UP, "Up"))[0].size == D(
        "2"
    )
