"""End-to-end paths that were only exercised live before: collect, record_market, sweep,
and the CLI wrappers around them, all with in-process fakes (no network)."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import ClassVar

from polymarket.models.clob.market_events import parse_market_event
from polymarket.models.gamma.market import Market as SdkMarket

from paperfill.cli import main
from paperfill.collect import collect
from paperfill.markets import from_gamma_json
from paperfill.recorder import JsonlSink, record_market
from paperfill.runner import RunConfig
from paperfill.sweep import sweep


def _book_event(token, ts_ms):
    return parse_market_event(
        {"event_type": "book", "market": "0x" + "1" * 64, "asset_id": token,
         "timestamp": str(ts_ms), "hash": "h",
         "bids": [{"price": "0.40", "size": "100"}], "asks": [{"price": "0.42", "size": "100"}]}
    )  # fmt: skip


class _AsyncClient:
    """Fake AsyncPublicClient: `subscribe` yields a few events then ends."""

    instances: ClassVar[list] = []

    def __init__(self, events):
        self._events = events
        self.closed = False
        _AsyncClient.instances.append(self)

    def subscribe(self, specs):
        self.specs = specs

        @asynccontextmanager
        async def cm():
            async def gen():
                for e in self._events:
                    yield e

            yield gen()

        return cm()

    async def close(self):
        self.closed = True


def test_record_market_subscribes_to_market_and_prices_and_closes_clients(tmp_path):
    events = [_book_event("1", 1782753357257), _book_event("2", 1782753357258)]
    _AsyncClient.instances.clear()
    sink = JsonlSink(tmp_path / "r.jsonl.gz")

    async def run():
        return await record_market(
            lambda: _AsyncClient(events), ["1", "2"], sink, seconds=0.2,
            reconnect_delay=0.01, price_symbols=("btcusdt", "btc/usd"), max_bytes=10**9,
        )  # fmt: skip

    n = asyncio.run(run())
    sink.close()
    assert n >= 2
    specs = _AsyncClient.instances[0].specs
    names = [type(s).__name__ for s in specs]
    assert names == [
        "MarketSpec",
        "CryptoPricesSpec",
        "CryptoPricesSpec",
        "CryptoPricesChainlinkTwapSpec",
    ]
    assert all(c.closed for c in _AsyncClient.instances)


class _Page:
    def __init__(self, items):
        self.items = tuple(items)


def _source_factory(raws):
    class Source:
        def list_markets(self, **params):
            return [_Page(SdkMarket.model_validate(r) for r in raws)]

        def close(self):
            pass

    return Source


def test_collect_records_windows_until_stop_file(tmp_path, gamma_market_raw, monkeypatch):
    now = datetime.now(UTC)
    raw = dict(gamma_market_raw)
    raw["endDate"] = (now + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw["slug"] = "btc-updown-5m-1"
    stop = tmp_path / "STOP"
    logs = []

    def fake_sleep(_s):
        stop.write_text("x")  # end after the first cycle

    # make the recording itself instant: patch record_market used by collect
    import paperfill.collect as collect_mod

    async def fake_record(*args, **kwargs):
        sink = args[2]
        sink.write({"kind": "start", "recv_ts": now.isoformat()})
        stop.write_text("x")
        return 1

    monkeypatch.setattr(collect_mod, "record_market", fake_record)
    n = collect(
        _source_factory([raw]), lambda: None, asset="BTC", window="5m",
        data_dir=tmp_path, hours=1, log=logs.append, sleep=fake_sleep,
    )  # fmt: skip
    assert n == 1 and any("events" in line for line in logs) and "STOP" in logs[-1]
    assert list(tmp_path.glob("*.jsonl.gz"))


def test_collect_waits_when_no_market_has_enough_lead(tmp_path, gamma_market_raw):
    raw = dict(gamma_market_raw)
    raw["endDate"] = (datetime.now(UTC) + timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw["slug"] = "btc-updown-5m-1"
    logs, waits = [], []

    def fake_sleep(s):
        waits.append(s)
        (tmp_path / "STOP").write_text("x")

    n = collect(
        _source_factory([raw]), lambda: None, asset="BTC", window="5m",
        data_dir=tmp_path, hours=1, log=logs.append, sleep=fake_sleep,
    )  # fmt: skip
    assert n == 0 and waits == [30] and "waiting" in logs[0]


def _write_recording(path: Path, market, start_offset_s=0):
    sink = JsonlSink(path)
    t = lambda s: (market.window_start + timedelta(seconds=s)).isoformat()  # noqa: E731
    sink.write({"kind": "start", "recv_ts": t(start_offset_s), "schema": 1})
    sink.write(
        {
            "kind": "prices.crypto.chainlink.twap",
            "recv_ts": t(0),
            "payload": {
                "symbol": "btc/usd",
                "value": "80000",
                "timestamp": 0,
                "window_seconds": 60,
            },
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.binance",
            "recv_ts": t(0),
            "payload": {"symbol": "btcusdt", "value": "80000", "timestamp": 0},
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.binance",
            "recv_ts": t(1),
            "payload": {"symbol": "btcusdt", "value": "80040", "timestamp": 1},
        }
    )
    for token, bid, ask in ((market.yes_token, "0.60", "0.62"), (market.no_token, "0.38", "0.40")):
        sink.write(
            {
                "kind": "book",
                "recv_ts": t(2),
                "payload": {
                    "token_id": token,
                    "bids": [{"price": bid, "size": "100"}],
                    "asks": [{"price": ask, "size": "100"}],
                },
            }
        )
    sink.write(
        {
            "kind": "prices.crypto.chainlink.twap",
            "recv_ts": t(10),
            "payload": {
                "symbol": "btc/usd",
                "value": "80100",
                "timestamp": 10,
                "window_seconds": 60,
            },
        }
    )
    sink.write(
        {
            "kind": "last_trade_price",
            "recv_ts": t(20),
            "payload": {
                "token_id": market.yes_token,
                "price": "0.58",
                "size": "10",
                "side": "SELL",
            },
        }
    )
    sink.write({"kind": "stop", "recv_ts": (market.end + timedelta(seconds=5)).isoformat()})
    sink.close()


def test_sweep_end_to_end_picks_on_train_and_reports_test(tmp_path, gamma_market_raw):
    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    m = from_gamma_json(raw)
    recs = []
    for i in range(4):
        p = tmp_path / f"{m.condition_id}-20260918T21500{i}Z.jsonl.gz"
        _write_recording(p, m)
        recs.append(p)
    grid = {
        "min_edge": ["0.02"],
        "edge_gain": ["10", "5"],
        "shrink_to_mid": ["0"],
        "stop_after_s": [0],
        "vol_sample_seconds": [0.0],
    }
    logs = []
    result = sweep(
        recs, lambda cid: m if cid == m.condition_id else None, strategy="fair-value", grid=grid,
        size="5", base_config=RunConfig(capital=D("100")), out_dir=tmp_path / "sweep",
        train_share=0.5, workers=1, log=logs.append,
    )  # fmt: skip
    assert result["train_windows"] == 2 and result["test_windows"] == 2
    assert len(result["train"]) == 2 and result["best"] is not None
    assert result["test"]["settled"] == 2
    md = (tmp_path / "sweep" / "sweep.md").read_text()
    assert "Chosen on train" in md and "On test" in md
    assert (tmp_path / "sweep" / "sweep.json").exists() and any(
        "best on train" in line for line in logs
    )


def test_cli_batch_calibrate_sweep_over_a_fake_directory(capsys, tmp_path, gamma_market_raw):
    from tests.test_cli import _fake_market_source

    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    m = from_gamma_json(raw)
    recdir = tmp_path / "rec"
    recdir.mkdir()
    for i in range(2):
        _write_recording(recdir / f"{m.condition_id}-20260918T21500{i}Z.jsonl.gz", m)
    Source = _fake_market_source(raw)
    out = tmp_path / "out"
    assert (
        main(
            [
                "batch",
                "--recordings",
                str(recdir),
                "--out",
                str(out),
                "--strategy",
                "two-sided",
                "--workers",
                "1",
            ],
            source_factory=Source,
        )
        == 0
    )
    assert "| windows | 2 |" in capsys.readouterr().out
    assert (
        main(
            [
                "calibrate",
                "--recordings",
                str(recdir),
                "--out",
                str(out / "cal"),
                "--offsets",
                "10,20",
            ],
            source_factory=Source,
        )
        == 0
    )
    assert "Brier" in capsys.readouterr().out and (out / "cal" / "calibration.json").exists()
    grid = tmp_path / "grid.json"
    grid.write_text(json.dumps({"lookback_s": [30], "lean_gain": ["20", "5"]}))
    assert (
        main(
            [
                "sweep",
                "--recordings",
                str(recdir),
                "--out",
                str(out / "sw"),
                "--strategy",
                "two-sided",
                "--grid",
                str(grid),
                "--train-share",
                "0.5",
            ],
            source_factory=Source,
        )
        == 0
    )
    assert "paperfill sweep: two-sided" in capsys.readouterr().out


def test_cli_collect_returns_1_when_nothing_recorded(tmp_path, monkeypatch):
    import paperfill.collect as collect_mod

    monkeypatch.setattr(collect_mod, "collect", lambda *a, **k: 0)
    assert (
        main(["collect", "--hours", "0", "--data-dir", str(tmp_path)], source_factory=lambda: None)
        == 1
    )
