"""Pair detector on a hand-built recording; venue interface conformance."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.book import OrderBook
from paperfill.fees import FeeSchedule
from paperfill.markets import from_gamma_json
from paperfill.pairs import pair_quote, scan_recording, summarize, to_markdown
from paperfill.recorder import JsonlSink
from paperfill.venues import PolymarketVenue, Venue

TS = datetime(2026, 9, 19, tzinfo=UTC)


def _fees():
    return FeeSchedule.from_gamma(
        {"rate": "0.07", "exponent": 1, "takerOnly": True, "rebateRate": "0.2"}, fees_enabled=True
    )


def _book(token, ask, size="10"):
    return OrderBook.from_snapshot(
        {"token_id": token, "bids": [], "asks": [{"price": ask, "size": size}]}
    )


def test_pair_quote_by_hand():
    # asks 0.48 + 0.50 -> gross 0.02; fees 0.07*0.48*0.52 = 0.017472 -> 0.01747 and
    # 0.07*0.50*0.50 = 0.0175 -> 0.01750; total 0.03497 > gross: not profitable
    q = pair_quote(_book("u", "0.48"), _book("d", "0.50"), _fees(), TS)
    assert q.gross_per_pair == D("0.02") and q.fee_per_pair == D("0.03497")
    assert q.net_per_pair == D("0.02") - D("0.03497")
    # asks 0.45 + 0.50 -> gross 0.05; fees 0.017325 -> 0.01733 (HALF_UP) + 0.01750 -> net 0.01517
    q = pair_quote(_book("u", "0.45", "3"), _book("d", "0.50", "7"), _fees(), TS)
    assert q.net_per_pair == D("0.01517") and q.size == D("3")
    empty = OrderBook.from_snapshot({"token_id": "d", "bids": [], "asks": []})
    assert pair_quote(_book("u", "0.45"), empty, _fees(), TS) is None


def test_scan_counts_episodes_seconds_and_first_sight_value(tmp_path, gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    up, down = m.yes_token, m.no_token
    t = lambda s: (m.window_start + timedelta(seconds=s)).isoformat()  # noqa: E731
    rec = tmp_path / f"{m.condition_id}-20260918T215000Z.jsonl.gz"
    sink = JsonlSink(rec)

    def book(ts, token, ask, size):
        sink.write({"kind": "book", "recv_ts": ts, "payload": {"token_id": token, "bids": [],
                    "asks": [{"price": ask, "size": size}]}})  # fmt: skip

    def change(ts, token, price, size):
        ch = {"token_id": token, "price": price, "size": size, "side": "SELL"}
        sink.write({"kind": "price_change", "recv_ts": ts, "payload": {"price_changes": [ch]}})

    book(t(0), up, "0.55", "10")
    book(t(1), down, "0.50", "4")  # 1.05: no
    change(t(5), up, "0.45", "6")  # 0.95: yes, size min(6, 4) = 4
    change(t(8), up, "0.45", "0")  # back to 0.55: no
    change(t(20), down, "0.40", "9")  # 0.95 again: yes, size min(10, 9) = 9
    sink.write({"kind": "stop", "recv_ts": t(30)})
    sink.close()
    scan = scan_recording(rec, m)
    # the first book (Up only) yields no pair; the other four events do
    assert scan.samples == 4 and scan.profitable == 2 and scan.episodes == 2
    assert scan.seconds_profitable == D("3")  # t5..t8; the second episode is open at the end
    # second episode: 0.55 + 0.40 -> gross 0.05; fees 0.07*0.55*0.45 = 0.017325 -> 0.01733 and
    # 0.07*0.40*0.60 = 0.0168 -> 0.03413; net 0.01587, size 9
    assert scan.best.net_per_pair == D("0.01587") and scan.best.size == D("9")
    assert scan.total_net == D("0.01517") * 4 + D("0.01587") * 9
    s = summarize([scan])
    assert s["windows_with_profitable_pair"] == 1 and s["episodes"] == 2
    assert s["best_net_per_pair"] == "0.01587"
    md = to_markdown([scan], s)
    assert "| Summary |" in md and m.question in md


def test_polymarket_venue_conforms_to_the_protocol(gamma_market_raw):
    from polymarket.models.gamma.market import Market as SdkMarket

    class Page:
        def __init__(self, items):
            self.items = tuple(items)

    class Paginator:
        def __init__(self, page):
            self._page = page

        def first_page(self):
            return self._page

        def __iter__(self):
            yield self._page

    class Client:
        def list_markets(self, **params):
            return Paginator(Page((SdkMarket.model_validate(gamma_market_raw),)))

        def close(self):
            pass

    venue = PolymarketVenue(client_factory=Client)
    assert isinstance(venue, Venue) and venue.name == "polymarket"
    m = venue.market(gamma_market_raw["conditionId"])
    assert m is not None and m.asset == "BTC"
    found = venue.discover(asset="BTC", window="5m", limit=5)
    assert [x.slug for x in found] == [gamma_market_raw["slug"]]


def test_pairs_cli_and_mcp_tool_over_a_fake_directory(capsys, tmp_path, gamma_market_raw):
    import json

    from tests.test_cli import _fake_market_source, main

    m = from_gamma_json(gamma_market_raw)
    recdir = tmp_path / "rec"
    recdir.mkdir()
    rec = recdir / f"{m.condition_id}-20260918T215000Z.jsonl.gz"
    sink = JsonlSink(rec)
    t = lambda s: (m.window_start + timedelta(seconds=s)).isoformat()  # noqa: E731
    for token, ask in ((m.yes_token, "0.45"), (m.no_token, "0.50")):
        sink.write({"kind": "book", "recv_ts": t(0), "payload": {"token_id": token, "bids": [],
                    "asks": [{"price": ask, "size": "5"}]}})  # fmt: skip
    sink.write({"kind": "stop", "recv_ts": t(5)})
    sink.close()
    Source = _fake_market_source(gamma_market_raw)
    out = tmp_path / "out"
    assert (
        main(["pairs", "--recordings", str(recdir), "--out", str(out)], source_factory=Source) == 0
    )
    text = capsys.readouterr().out
    assert "| windows_with_profitable_pair | 1 |" in text
    body = json.loads((out / "pairs.json").read_text())
    assert body["windows"][0]["best_net_per_pair"] == "0.01517"
    assert main(["pairs", "--recordings", str(tmp_path / "empty")], source_factory=Source) == 1

    import pytest

    pytest.importorskip("mcp")
    import asyncio

    from mcp import Client

    from paperfill.mcp_server import create_server

    server = create_server(data_dir=tmp_path, source_factory=Source)

    async def go(args):
        async with Client(server) as c:
            return await c.call_tool("scan_pairs", args)

    r = asyncio.run(go({"recordings_dir": str(recdir)}))
    assert not r.is_error and r.structured_content["summary"]["episodes"] == 1
    assert asyncio.run(go({"recordings_dir": str(tmp_path / "none")})).is_error


def test_polymarket_venue_book_trades_and_record_with_fakes(
    tmp_path, gamma_market_raw, monkeypatch
):
    from polymarket.models.data.activity import Trade as SdkTrade
    from polymarket.models.gamma.market import Market as SdkMarket

    m = from_gamma_json(gamma_market_raw)

    class Book:
        def model_dump(self, mode="json"):
            return {"token_id": m.yes_token, "bids": [{"price": "0.5", "size": "1"}], "asks": []}

    class Page:
        def __init__(self, items, has_more=False):
            self.items, self.has_more, self.next_cursor = tuple(items), has_more, None

    class Paginator:
        def __init__(self, items):
            self._items = items

        def first_page(self):
            return Page(self._items)

        def __iter__(self):
            yield Page(self._items)

    row = {"proxy_wallet": "0x" + "ab" * 20, "side": "BUY", "token_id": m.yes_token,
           "condition_id": m.condition_id, "size": "5", "price": "0.6", "timestamp": 1000,
           "transaction_hash": "0x" + "cd" * 32}  # fmt: skip

    class Client:
        def get_order_book(self, token_id):
            return Book()

        def list_trades(self, **params):
            return Paginator((SdkTrade.model_validate(row),))

        def list_markets(self, **params):
            return Paginator((SdkMarket.model_validate(gamma_market_raw),))

        def close(self):
            pass

    venue = PolymarketVenue(client_factory=Client)
    assert venue.order_book(m.yes_token).best_bid.price == D("0.5")
    assert [t.price for t in venue.trades(m.condition_id)] == [D("0.6")]

    import paperfill.venues as venues_mod

    async def fake_record(*args, **kwargs):
        args[2].write({"kind": "start", "recv_ts": "2026-09-19T00:00:00+00:00"})
        return 1

    monkeypatch.setattr("paperfill.recorder.record_market", fake_record)
    n = venue.record(m, tmp_path / "r.jsonl.gz", seconds=1, with_prices=True)
    assert n == 1 and (tmp_path / "r.jsonl.gz").exists()
    assert (
        venues_mod.first_event_time(tmp_path / "r.jsonl.gz") is None
    )  # start records are not events
