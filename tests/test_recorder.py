"""Recorder: serialisation of documented events, gaps and reconnection, stop."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from polymarket.models.clob.market_events import parse_market_event

from paperfill.recorder import ListSink, event_record, run_recorder

# Wire-format examples verbatim from polymarket/realtime-data.md (API tab), parsed by the
# SDK's own `parse_market_event`, which is what the live stream goes through.
MARKET = "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75"
TOKEN = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
BOOK = {
    "event_type": "book",
    "market": MARKET,
    "asset_id": TOKEN,
    "timestamp": "1782753357257",
    "hash": "0xabc123",
    "bids": [{"price": "0.08", "size": "33343.4"}, {"price": "0.09", "size": "163939.58"}],
    "asks": [{"price": "0.99", "size": "218442.27"}, {"price": "0.98", "size": "13229.55"}],
}
PRICE_CHANGE = {
    "event_type": "price_change",
    "market": MARKET,
    "price_changes": [
        {
            "asset_id": TOKEN,
            "price": "0.08",
            "size": "33343.4",
            "side": "BUY",
            "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
            "best_bid": "0.08",
            "best_ask": "0.09",
        }
    ],
    "timestamp": "1782753357257",
}
LAST_TRADE = {
    "event_type": "last_trade_price",
    "market": MARKET,
    "asset_id": TOKEN,
    "price": "0.08",
    "size": "219.217767",
    "fee_rate_bps": "0",
    "side": "SELL",
    "timestamp": "1782753357257",
    "transaction_hash": "0xeeefff",
}

T0 = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)


def _events():
    return [
        parse_market_event(BOOK),
        parse_market_event(PRICE_CHANGE),
        parse_market_event(LAST_TRADE),
    ]


def test_documented_events_serialise_without_hashes():
    book, change, trade = (event_record(e, T0) for e in _events())
    assert book["kind"] == "book" and book["recv_ts"] == T0.isoformat()
    assert book["payload"]["bids"][0] == {"price": "0.08", "size": "33343.4"}
    assert book["payload"]["timestamp"] == "2026-06-29T17:15:57.257000Z"  # epoch ms decoded
    assert change["kind"] == "price_change"
    assert change["payload"]["price_changes"][0]["best_ask"] == "0.09"
    assert trade["kind"] == "last_trade_price" and trade["payload"]["price"] == "0.08"
    assert "transaction_hash" not in trade["payload"]


class _Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


def _subscribe_factory(scenarios):
    """Each call pops one scenario: a list of events, or an exception to raise."""
    calls = []

    def subscribe():
        calls.append(1)
        scenario = scenarios.pop(0)

        @asynccontextmanager
        async def cm():
            if isinstance(scenario, Exception):
                raise scenario

            async def gen():
                for e in scenario:
                    yield e

            yield gen()

        return cm()

    return subscribe, calls


async def _no_sleep(_):
    return None


def test_reconnects_after_failure_and_writes_gap():
    sink = ListSink()
    stop = asyncio.Event()
    subscribe, calls = _subscribe_factory([ConnectionError("socket closed"), _events()])
    n = asyncio.run(
        run_recorder(subscribe, sink, stop=stop, clock=_Clock(), sleep=_no_sleep, max_events=3)
    )
    kinds = [r["kind"] for r in sink.records]
    assert kinds == ["start", "gap", "book", "price_change", "last_trade_price", "stop"]
    assert sink.records[1]["reason"] == "ConnectionError: socket closed"
    assert n == 3 and len(calls) == 2 and sink.records[-1]["events"] == 3


def test_stream_that_ends_by_itself_is_a_gap_then_reconnect():
    sink = ListSink()
    stop = asyncio.Event()
    subscribe, _calls = _subscribe_factory([_events()[:1], _events()[1:]])
    asyncio.run(
        run_recorder(subscribe, sink, stop=stop, clock=_Clock(), sleep=_no_sleep, max_events=3)
    )
    kinds = [r["kind"] for r in sink.records]
    assert kinds == ["start", "book", "gap", "price_change", "last_trade_price", "stop"]
    assert sink.records[2]["reason"] == "stream ended"


def test_stop_event_ends_loop_without_gap():
    sink = ListSink()
    stop = asyncio.Event()

    async def scenario():
        subscribe, _ = _subscribe_factory([_events()])

        async def stopper():
            await asyncio.sleep(0)
            stop.set()

        asyncio.get_running_loop().create_task(stopper())
        return await run_recorder(
            subscribe, sink, stop=stop, clock=_Clock(), sleep=_no_sleep, max_events=1
        )

    asyncio.run(scenario())
    assert [r["kind"] for r in sink.records] == ["start", "book", "stop"]
