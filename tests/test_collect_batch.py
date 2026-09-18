"""Gzip recordings, window planning, batch aggregation."""

import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from polymarket.models.gamma.market import Market as SdkMarket

from paperfill.batch import WindowResult, condition_of, run_batch, summarize, to_markdown
from paperfill.collect import next_plan
from paperfill.markets import from_gamma_json
from paperfill.recorder import JsonlSink, open_text
from paperfill.report import Metrics
from paperfill.runner import RunConfig, events_from_recording, recording_covers_resolution
from paperfill.strategy import TakerProbe


def test_gzip_sink_roundtrip_and_events(tmp_path):
    path = tmp_path / "r.jsonl.gz"
    sink = JsonlSink(path)
    sink.write({"kind": "start", "recv_ts": "2026-09-19T00:00:00+00:00"})
    sink.write(
        {
            "kind": "book",
            "recv_ts": "2026-09-19T00:00:01+00:00",
            "payload": {"token_id": "t", "bids": [{"price": "0.5", "size": "1"}], "asks": []},
        }
    )
    sink.close()
    with gzip.open(path, "rt") as f:
        assert len(f.read().splitlines()) == 2
    with open_text(path) as f:
        assert json.loads(f.readline())["kind"] == "start"
    assert [type(e).__name__ for e in events_from_recording(path)] == ["BookEvent"]
    assert recording_covers_resolution(path, datetime(2026, 9, 19, 0, 0, 1, tzinfo=UTC))
    assert not recording_covers_resolution(path, datetime(2026, 9, 19, 0, 0, 2, tzinfo=UTC))


class _Page:
    def __init__(self, items):
        self.items = tuple(items)


class _Source:
    def __init__(self, raws):
        self.raws = raws

    def list_markets(self, **params):
        return [_Page(SdkMarket.model_validate(r) for r in self.raws)]


def _variant(raw, end, slug):
    v = dict(raw)
    v["endDate"], v["slug"] = end, slug
    return v


def test_next_plan_picks_soonest_window_with_enough_lead(gamma_market_raw):
    now = datetime(2026, 9, 18, 21, 53, 30, tzinfo=UTC)
    src = _Source(
        [
            _variant(gamma_market_raw, "2026-09-18T21:55:00Z", "btc-updown-5m-1"),  # 90 s left
            _variant(gamma_market_raw, "2026-09-18T22:00:00Z", "btc-updown-5m-2"),  # 390 s left
        ]
    )
    plan = next_plan(src, asset="BTC", window="5m", now=now, min_lead=120, after_end=30)
    assert plan.market.slug == "btc-updown-5m-2" and plan.seconds == 420
    plan = next_plan(src, asset="BTC", window="5m", now=now, min_lead=60, after_end=30)
    assert plan.market.slug == "btc-updown-5m-1" and plan.seconds == 120
    assert next_plan(src, asset="ETH", window="5m", now=now) is None


def test_condition_of_recording_names():
    cid = "0x" + "ab" * 32
    assert condition_of(Path(f"{cid}-20260918T224252Z.jsonl.gz")) == cid
    assert condition_of(Path(f"{cid}-20260918T224252Z.jsonl")) == cid
    assert condition_of(Path("notes.txt")) is None


def test_batch_runs_taker_probe_and_summarises(tmp_path, gamma_market_raw):
    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    market = from_gamma_json(raw)
    cid = market.condition_id
    rec = tmp_path / f"{cid}-20260918T215000Z.jsonl.gz"
    sink = JsonlSink(rec)
    t = lambda s: (market.window_start + timedelta(seconds=s)).isoformat()  # noqa: E731
    for token, bid, ask in ((market.yes_token, "0.60", "0.62"), (market.no_token, "0.38", "0.40")):
        sink.write(
            {
                "kind": "book",
                "recv_ts": t(0),
                "payload": {
                    "token_id": token,
                    "bids": [{"price": bid, "size": "100"}],
                    "asks": [{"price": ask, "size": "100"}],
                },
            }
        )
    sink.write({"kind": "stop", "recv_ts": (market.end + timedelta(seconds=5)).isoformat()})
    sink.close()
    results = run_batch(
        [rec, tmp_path / "junk.txt"],
        lambda c: market if c == cid else None,
        lambda: TakerProbe(size=D("5")),
        config=RunConfig(capital=D("100")),
        out_dir=tmp_path / "out",
        log=lambda _: None,
    )
    assert len(results) == 1 and results[0].settled
    m = results[0].metrics
    # 5 Up @ 0.62 and 5 Down @ 0.40 as taker, crypto fees 0.07:
    #   5*0.07*0.62*0.38 = 0.08246; 5*0.07*0.40*0.60 = 0.084 -> total 0.16646
    assert m.taker_fills == 2 and m.fees == D("0.16646")
    # settlement Up=1: 5 - (3.10 + 0.08246) = 1.81754; Down=0: -(2.00 + 0.084) = -2.084
    assert m.realized_pnl == D("1.81754") - D("2.084")
    # per-token settled P&L is after fees too, so it adds up to the realized figure
    assert sum(D(t["settled_pnl"]) for t in m.per_token.values()) == m.realized_pnl
    summary = summarize(results)
    assert summary["settled"] == 1 and summary["total_fees"] == "0.16646"
    md = to_markdown(results, summary, "taker-probe")
    assert (
        "| total_fees | 0.16646 |" in md
        and (tmp_path / "out" / rec.name.split(".")[0] / "report.json").exists()
    )


def test_summarize_handles_unsettled_only():
    r = WindowResult("x", "q", False, Metrics())
    s = summarize([r])
    assert s["settled"] == 0 and s["mean_pnl"] is None and s["windows"] == 1
