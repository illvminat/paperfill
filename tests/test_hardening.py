"""Block 2: schema versions, resume, disk cap, config precedence, health, logs."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from paperfill import schema
from paperfill.config import Settings
from paperfill.dashboard import create_app
from paperfill.history import TradePrint
from paperfill.journal import Journal, read_entries, verify
from paperfill.markets import from_gamma_json
from paperfill.recorder import JsonlSink, ListSink, run_recorder
from paperfill.report import compute
from paperfill.runner import RunConfig, Runner, events_from_recording
from paperfill.strategy import TwoSidedQuoter

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_schema_check_accepts_legacy_and_current_refuses_newer():
    assert schema.check("x", {}, 1) == 1
    assert schema.check("x", {"schema": 1}, 1) == 1
    with pytest.raises(schema.SchemaError, match="newer"):
        schema.check("x", {"schema": 2}, 1)
    with pytest.raises(schema.SchemaError):
        schema.check("x", {"schema": "one"}, 1)


def test_recording_start_carries_schema_and_reader_refuses_future(tmp_path):
    from contextlib import asynccontextmanager

    from polymarket.models.clob.market_events import parse_market_event

    book = parse_market_event(
        {"event_type": "book", "market": "0x" + "1" * 64, "asset_id": "1",
         "timestamp": "1782753357257", "hash": "h", "bids": [], "asks": []}
    )  # fmt: skip

    def subscribe():
        @asynccontextmanager
        async def cm():
            async def gen():
                yield book

            yield gen()

        return cm()

    sink = ListSink()
    asyncio.run(run_recorder(subscribe, sink, stop=asyncio.Event(), max_events=1, sleep=_no_sleep))
    start = sink.records[0]
    assert start["kind"] == "start" and start["schema"] == schema.RECORDING_SCHEMA
    future = tmp_path / "f.jsonl"
    future.write_text('{"kind": "start", "recv_ts": "2026-09-19T00:00:00+00:00", "schema": 99}\n')
    with pytest.raises(schema.SchemaError):
        list(events_from_recording(future))


async def _no_sleep(_):
    return None


def _empty_stream():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cm():
        async def gen():
            return
            yield  # pragma: no cover

        yield gen()

    return cm()


def test_disk_cap_stops_recording_with_reason(tmp_path):
    from contextlib import asynccontextmanager

    from polymarket.models.clob.market_events import parse_market_event

    book = parse_market_event(
        {"event_type": "book", "market": "0x" + "1" * 64, "asset_id": "1",
         "timestamp": "1782753357257",
         "hash": "h", "bids": [{"price": "0.5", "size": "1"}], "asks": []}
    )  # fmt: skip

    def subscribe():
        @asynccontextmanager
        async def cm():
            async def gen():
                for _ in range(1000):
                    yield book

            yield gen()

        return cm()

    sink = JsonlSink(tmp_path / "r.jsonl")
    n = asyncio.run(
        run_recorder(subscribe, sink, stop=asyncio.Event(), sleep=_no_sleep, max_bytes=2000)
    )
    sink.close()
    lines = [json.loads(line) for line in (tmp_path / "r.jsonl").read_text().splitlines()]
    assert (
        lines[-1]["kind"] == "stop"
        and "disk cap" in lines[-1]["reason"]
        and lines[-1]["events"] == n
    )
    assert n < 1000 and sink.bytes_written >= 2000


def _tape(m):
    up, down = m.yes_token, m.no_token
    t = lambda s: T0 + timedelta(seconds=s)  # noqa: E731
    return [
        TradePrint(t(0), 0, "BUY", D("0.60"), D("50"), up, "Up"),
        TradePrint(t(0), 1, "BUY", D("0.40"), D("50"), down, "Down"),
        TradePrint(t(6), 2, "SELL", D("0.58"), D("3"), up, "Up"),
        TradePrint(t(7), 3, "SELL", D("0.38"), D("3"), down, "Down"),
        TradePrint(t(30), 4, "BUY", D("0.61"), D("1"), up, "Up"),
        TradePrint(t(40), 5, "SELL", D("0.57"), D("2"), up, "Up"),
        TradePrint(t(50), 6, "BUY", D("0.62"), D("1"), up, "Up"),
    ]


def _clock():
    t = [T0]

    def tick():
        t[0] += timedelta(milliseconds=1)
        return t[0]

    return tick


def test_resume_after_kill_matches_uninterrupted_run(tmp_path, gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    tape = _tape(m)
    strat = lambda: TwoSidedQuoter(  # noqa: E731
        size=D("5"), requote_every=timedelta(seconds=5), quote_ttl=None
    )

    # reference: one uninterrupted run
    j_ref = Journal(clock=_clock())
    ref = Runner(m, strat(), j_ref, RunConfig(capital=D("100")), mode="replay").run(tape)

    # interrupted: kill file appears after the 4th event, run halts; then resume
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    kill = run_dir / "KILL"
    cfg = RunConfig(capital=D("100"), kill_file=kill)
    j1 = Journal.open(run_dir / "journal.jsonl", clock=_clock())
    r1 = Runner(m, strat(), j1, cfg, mode="replay")

    def with_kill():
        for i, e in enumerate(tape):
            if i == 4:
                kill.write_text("stop")
            yield e

    first = r1.run(with_kill())
    j1.close()
    assert first.halted == "kill switch file present" and first.events < ref.events
    marks = [e for e in read_entries(run_dir / "journal.jsonl") if e.kind == "mark"]
    assert all("events" in e.data and "last_ts" in e.data for e in marks)

    kill.unlink()
    j2 = Journal.open(run_dir / "journal.jsonl", clock=_clock())
    r2 = Runner(m, strat(), j2, cfg, mode="replay", resume=True)
    second = r2.run(tape)
    j2.close()
    entries = read_entries(run_dir / "journal.jsonl")
    assert verify(entries).ok
    kinds = [e.kind for e in entries]
    assert kinds.count("run_end") == 2 and "resume" in kinds and "resume_skipped" in kinds
    assert second.events == ref.events  # every event of the tape was processed exactly once
    # what may differ: fills from orders that were resting at the halt (not restored, documented)
    ref_fills, res_fills = len(ref.fills), len(second.fills)
    assert res_fills <= ref_fills
    # what must hold: the rebuilt portfolio reconciles with the journal's own fills
    fills = [e.data for e in entries if e.kind == "fill"]
    paid = sum(D(f["size"]) * D(f["price"]) + D(f["fee"]) for f in fills if f["side"] == "BUY")
    assert second.portfolio.cash == D("100") - paid
    assert sum(p.size for p in second.portfolio.positions.values()) == sum(
        D(f["size"]) for f in fills if f["side"] == "BUY"
    )
    metrics = compute(entries)
    assert metrics.fills == res_fills and metrics.events == ref.events


def test_resume_refuses_wrong_market_or_settled(tmp_path, gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    j = Journal.open(tmp_path / "journal.jsonl", clock=_clock())
    Runner(m, TwoSidedQuoter(), j, RunConfig(), mode="replay").run(
        [], payouts={m.yes_token: D("1"), m.no_token: D("0")}
    )
    j.close()
    j2 = Journal.open(tmp_path / "journal.jsonl", clock=_clock())
    with pytest.raises(ValueError, match="already settled"):
        Runner(m, TwoSidedQuoter(), j2, RunConfig(), mode="replay", resume=True)
    other = dict(gamma_market_raw)
    other["conditionId"] = "0x" + "9" * 64
    with pytest.raises(ValueError, match="another market"):
        Runner(
            from_gamma_json(other), TwoSidedQuoter(), j2, RunConfig(), mode="replay", resume=True
        )


def test_settings_precedence_file_then_flag(tmp_path):
    cfg = tmp_path / "p.toml"
    cfg.write_text(
        '[run]\ncapital = "250"\nstrategy = "fair-value"\n'
        '[risk]\ndaily_loss = "5"\n[model]\nvol_sample_seconds = 60.0\n'
    )
    s = Settings.load(cfg)
    assert s.decimal("run", "capital") == D("250") and s.get("run", "strategy") == "fair-value"
    assert (
        s.decimal("risk", "daily_loss") == D("5") and s.get("model", "vol_sample_seconds") == 60.0
    )
    assert s.decimal("risk", "total_loss") == D("50")  # untouched default
    s.override("run", "capital", D("300"))
    s.override("run", "strategy", None)  # flag not given: file value stays
    assert s.decimal("run", "capital") == D("300") and s.get("run", "strategy") == "fair-value"
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\nx = 1\n")
    with pytest.raises(ValueError, match="unknown section"):
        Settings.load(bad)
    bad.write_text("[run]\ncapitol = 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        Settings.load(bad)


def test_run_reads_config_file_and_flags_win(capsys, tmp_path, gamma_market_raw):
    from tests.test_cli import _fake_market_source, main

    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    toks = json.loads(raw["clobTokenIds"])
    rows = [
        {"proxy_wallet": "0x" + "ab" * 20, "side": "BUY", "token_id": toks[0],
         "condition_id": raw["conditionId"], "size": "50", "price": "0.60",
         "timestamp": 1000, "transaction_hash": "0x" + "cd" * 32}
    ]  # fmt: skip
    cfg = tmp_path / "p.toml"
    cfg.write_text('[run]\ncapital = "250"\n')
    argv = ["run", "--condition", raw["conditionId"], "--runs-dir", str(tmp_path / "runs"),
            "--history-dir", str(tmp_path / "h"),
            "--config", str(cfg), "--capital", "300"]  # fmt: skip
    assert main(argv, source_factory=_fake_market_source(raw, rows)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    start = read_entries(run_dir / "journal.jsonl")[0]
    assert start.data["capital"] == "300" and start.data["schema"] == schema.JOURNAL_SCHEMA


def test_healthz_reports_state(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/healthz").json() == {"status": "ok", "state": "empty", "entries": 0}
    (tmp_path / "journal.jsonl").write_text("not json\n")
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["status"] == "error"


def test_json_log_lines_on_stderr(capsys):
    from paperfill.log import get, setup

    setup("INFO")
    get("t").info("hello", extra={"data": {"k": 1}})
    logging.getLogger("paperfill").handlers[0].flush()
    err = capsys.readouterr().err.strip().splitlines()[-1]
    rec = json.loads(err)
    assert rec["msg"] == "hello" and rec["k"] == 1 and rec["level"] == "INFO"
