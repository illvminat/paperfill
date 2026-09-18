"""Order book: recorded REST snapshot, incremental changes, best levels."""

from decimal import Decimal as D

from paperfill.book import Level, OrderBook


def test_rest_snapshot_parses_and_best_levels_are_extremes(clob_book_raw):
    book = OrderBook.from_snapshot(clob_book_raw)
    assert book.token_id == clob_book_raw["asset_id"]
    assert book.tick_size == D("0.01") and book.min_order_size == D("5")
    assert len(book.bids) == len(clob_book_raw["bids"]) and len(book.asks) == len(
        clob_book_raw["asks"]
    )
    assert book.best_bid.price == max(D(lv["price"]) for lv in clob_book_raw["bids"])
    assert book.best_ask.price == min(D(lv["price"]) for lv in clob_book_raw["asks"])
    assert book.best_bid.price < book.best_ask.price
    assert book.spread == book.best_ask.price - book.best_bid.price
    assert book.mid == (book.best_ask.price + book.best_bid.price) / 2
    assert book.asks_ascending()[0] == book.best_ask and book.bids_descending()[0] == book.best_bid


def test_price_change_sets_new_size_and_zero_removes():
    book = OrderBook.from_snapshot(
        {
            "token_id": "t",
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "7"}],
        }
    )
    book.apply_price_change({"side": "BUY", "price": "0.51", "size": "3"})
    assert book.best_bid == Level(D("0.51"), D("3"))
    book.apply_price_change({"side": "BUY", "price": "0.51", "size": "0"})
    assert book.best_bid == Level(D("0.50"), D("10"))
    book.apply_price_change({"side": "SELL", "price": "0.52", "size": "1"})
    assert book.best_ask == Level(D("0.52"), D("1"))
    assert book.snapshot_levels() == ((("0.50", "10"),), (("0.52", "1"),))


def test_empty_sides_give_none():
    book = OrderBook.from_snapshot(
        {"token_id": "t", "bids": [], "asks": [{"price": "0.9", "size": "1"}]}
    )
    assert book.best_bid is None and book.mid is None and book.spread is None
    assert book.best_ask == Level(D("0.9"), D("1"))


def test_stream_snapshot_with_iso_timestamp_and_zero_size_levels_dropped():
    book = OrderBook.from_snapshot(
        {
            "asset_id": "t",
            "timestamp": "2026-06-29T17:15:57.257000Z",
            "bids": [{"price": "0.08", "size": "0"}, {"price": "0.07", "size": "5"}],
            "asks": [],
            "hash": "h",
        }
    )
    assert book.ts.year == 2026 and book.hash == "h"
    assert list(book.bids) == [D("0.07")]
