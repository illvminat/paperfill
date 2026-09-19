"""MCP server: paperfill as a set of tools for an agent (Claude Code, Claude Desktop, …).

Sources of truth: SOURCES.md -> mcp/sdk-*.md (MCPServer, tools, structured output,
errors, stdio) and mcp/spec-tools.md (names, annotations). Every tool returns a pydantic
model, so the client gets structured content with a schema. Nothing here can trade:
the tools wrap the same paper-only modules the CLI uses.

Run it with `paperfill mcp` (stdio). Logs go to stderr, as the stdio transport
requires (mcp/spec-stdio.md): stdout carries only protocol frames.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from paperfill import __version__

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
LOCAL_READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITES_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

Strategy = Literal["two-sided", "fair-value", "taker-probe"]


class MarketOut(BaseModel):
    condition_id: str
    question: str
    asset: str | None
    window: str | None
    ends_at: str | None
    seconds_left: int | None
    tick_size: str
    min_order_size: str
    fee_rate: str
    rebate_rate: str
    tradeable: bool


class HistoryOut(BaseModel):
    condition_id: str
    trades: int
    first_ts: str | None
    last_ts: str | None
    digest: str
    path: str


class RunOut(BaseModel):
    run_dir: str
    mode: str
    strategy: str
    events: int
    fills: int
    realized_pnl: str = Field(
        description="After all fees; settled at 1/0 when the market is resolved."
    )
    fees: str
    settled: bool
    halted: str | None
    report_markdown: str


class VerifyOut(BaseModel):
    ok: bool
    entries: int
    first_bad_seq: int | None
    reason: str | None


class ListingOut(BaseModel):
    directory: str
    names: list[str]


class BatchOut(BaseModel):
    strategy: str
    out_dir: str
    summary: dict[str, Any]
    markdown: str


class PairsOut(BaseModel):
    summary: dict[str, Any]
    windows: list[dict[str, Any]]
    markdown: str


class CalibrationOut(BaseModel):
    windows: int
    brier_model: dict[str, float | None]
    brier_mid: dict[str, float | None]
    markdown: str


def _dec(text: str, name: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ToolError(f"{name} must be a decimal number, got {text!r}") from error


def create_server(
    *,
    data_dir: Path = Path("data"),
    source_factory: Callable[[], Any] | None = None,
) -> MCPServer:
    """Build the server; `source_factory` is injectable so tests never touch the network."""
    from paperfill.cli import (
        _default_source,
        _load_market,
        _load_markets,
        _make_strategy,
        _run_config_from,
    )
    from paperfill.config import Settings

    factory = source_factory or _default_source
    runs_dir, history_dir, record_dir = data_dir / "runs", data_dir / "history", data_dir / "record"

    mcp = MCPServer(
        "paperfill",
        title="paperfill paper-trading harness",
        version=__version__,
        instructions=(
            "Paper-trading tools for Polymarket Up/Down markets. Nothing here places live "
            "orders. Typical flow: discover_markets -> fetch_history or list_recordings -> "
            "run_paper -> read_report. Reported P&L is a property of the strategy on that "
            "tape, not a forecast."
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    def discover_markets(
        asset: Annotated[str | None, Field(description="Asset symbol, e.g. BTC or ETH.")] = None,
        window: Annotated[str | None, Field(description="Window, e.g. 5m or 15m.")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[MarketOut]:
        """Open Up/Down markets, soonest to end first, with fees, tick and minimum size."""
        from paperfill.markets import discover

        client = factory()
        try:
            rows = list(discover(client, asset=asset, window=window, limit=limit))
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        now = datetime.now(UTC)
        return [
            MarketOut(
                condition_id=m.condition_id,
                question=m.question,
                asset=m.asset,
                window=m.window,
                ends_at=m.end.isoformat() if m.end else None,
                seconds_left=int((m.end - now).total_seconds()) if m.end else None,
                tick_size=str(m.tick_size),
                min_order_size=str(m.min_order_size),
                fee_rate=str(m.fees.rate),
                rebate_rate=str(m.fees.rebate_rate),
                tradeable=m.tradeable,
            )
            for m in rows
        ]

    @mcp.tool(annotations=WRITES_LOCAL)
    def fetch_history(
        condition_id: Annotated[str, Field(description="Market condition id (0x…).")],
        refresh: bool = False,
    ) -> HistoryOut:
        """Download (or reuse) the full trade history of a market; personal fields are dropped."""
        from paperfill.history import (
            fetch_trades,
            history_path,
            load_trades,
            replay,
            save_trades,
            stream_digest,
        )

        path = history_path(history_dir, condition_id)
        if path.exists() and not refresh:
            _, trades = load_trades(path)
        else:
            client = factory()
            try:
                trades = fetch_trades(client, condition_id)
            finally:
                close = getattr(client, "close", None)
                if close:
                    close()
            save_trades(path, condition_id, trades, fetched_at=datetime.now(UTC))
        stream = list(replay(trades))
        return HistoryOut(
            condition_id=condition_id,
            trades=len(stream),
            first_ts=stream[0].ts.isoformat() if stream else None,
            last_ts=stream[-1].ts.isoformat() if stream else None,
            digest=stream_digest(stream),
            path=str(path),
        )

    @mcp.tool(annotations=WRITES_LOCAL)
    def run_paper(
        condition_id: Annotated[str, Field(description="Market condition id (0x…).")],
        strategy: Strategy = "two-sided",
        recording: Annotated[
            str | None,
            Field(description="Recording file to run on; omit to replay the trade history."),
        ] = None,
        capital: str = "100",
        size: str = "5",
        config_toml: Annotated[
            str | None, Field(description="Optional TOML settings (same sections as --config).")
        ] = None,
    ) -> RunOut:
        """Paper-trade a sample strategy over a tape; journal and report go under data/runs."""
        from paperfill.history import fetch_trades, history_path, load_trades, replay, save_trades
        from paperfill.journal import Journal
        from paperfill.report import compute, to_json, to_markdown
        from paperfill.runner import Runner, events_from_recording, recording_covers_resolution

        market = _load_market(factory, condition_id)
        if market is None:
            raise ToolError(f"no market with condition id {condition_id}")
        settings = Settings.loads(config_toml) if config_toml else Settings()
        settings.override("run", "capital", _dec(capital, "capital"))
        settings.override("run", "size", _dec(size, "size"))
        settings.override("run", "strategy", strategy)
        payouts = market.payouts
        if recording is not None:
            rec = Path(recording)
            if not rec.exists():
                raise ToolError(f"recording not found: {recording}")
            mode, events = "record", events_from_recording(rec)
            if not recording_covers_resolution(rec, market.end):
                payouts = None
        else:
            mode = "replay"
            path = history_path(history_dir, condition_id)
            if path.exists():
                _, trades = load_trades(path)
            else:
                client = factory()
                try:
                    trades = fetch_trades(client, condition_id)
                finally:
                    close = getattr(client, "close", None)
                    if close:
                        close()
                save_trades(path, condition_id, trades, fetched_at=datetime.now(UTC))
            events = replay(trades)
        started = datetime.now(UTC)
        run_dir = runs_dir / f"{started.strftime('%Y%m%dT%H%M%S%fZ')}-{condition_id[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        config = _run_config_from(settings, run_dir / "KILL")
        journal = Journal.open(run_dir / "journal.jsonl")
        strat = _make_strategy(
            strategy,
            settings.decimal("run", "size"),
            settings.decimal("run", "min_edge"),
            settings.values["strategy"],
        )()
        try:
            result = Runner(market, strat, journal, config, mode=mode).run(events, payouts=payouts)
        finally:
            journal.close()
        metrics = compute(journal.entries)
        md = to_markdown(metrics)
        (run_dir / "report.md").write_text(md, encoding="utf-8")
        (run_dir / "report.json").write_text(to_json(metrics), encoding="utf-8")
        return RunOut(
            run_dir=str(run_dir),
            mode=mode,
            strategy=strat.name,
            events=result.events,
            fills=len(result.fills),
            realized_pnl=str(result.portfolio.realized_pnl),
            fees=str(result.portfolio.fees_paid),
            settled=result.settled,
            halted=result.halted,
            report_markdown=md,
        )

    @mcp.tool(annotations=LOCAL_READ)
    def read_report(run_dir: str) -> RunOut:
        """Rebuild a run's report from its journal (fails if the hash chain is broken)."""
        from paperfill.journal import read_entries, verify
        from paperfill.report import compute, to_markdown

        d = Path(run_dir)
        path = d / "journal.jsonl"
        if not path.exists():
            raise ToolError(f"no journal in {run_dir}")
        entries = read_entries(path)
        v = verify(entries)
        if not v.ok:
            raise ToolError(f"journal chain broken at seq {v.first_bad_seq}: {v.reason}")
        m = compute(entries)
        return RunOut(
            run_dir=str(d),
            mode=m.mode,
            strategy=m.strategy,
            events=m.events,
            fills=m.fills,
            realized_pnl=str(m.realized_pnl),
            fees=str(m.fees),
            settled=m.settled,
            halted=m.halted,
            report_markdown=to_markdown(m),
        )

    @mcp.tool(annotations=LOCAL_READ)
    def verify_journal(path: str) -> VerifyOut:
        """Verify the SHA-256 hash chain of a journal file."""
        from paperfill.journal import verify_file

        p = Path(path)
        if not p.exists():
            raise ToolError(f"file not found: {path}")
        v = verify_file(p)
        return VerifyOut(ok=v.ok, entries=v.entries, first_bad_seq=v.first_bad_seq, reason=v.reason)

    @mcp.tool(annotations=LOCAL_READ)
    def list_recordings() -> ListingOut:
        """Recording files available under data/record."""
        names = sorted(p.name for p in record_dir.glob("*.jsonl*")) if record_dir.is_dir() else []
        return ListingOut(directory=str(record_dir), names=names)

    @mcp.tool(annotations=LOCAL_READ)
    def list_runs() -> ListingOut:
        """Run directories under data/runs, newest last."""
        names = (
            sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
        )
        return ListingOut(directory=str(runs_dir), names=names)

    @mcp.tool(annotations=WRITES_LOCAL)
    def run_batch(
        strategy: Strategy = "fair-value",
        recordings_dir: str | None = None,
        workers: Annotated[int, Field(ge=1, le=32)] = 1,
        config_toml: str | None = None,
    ) -> BatchOut:
        """Run one strategy over every recording in a directory; summary with bootstrap CI."""
        from paperfill.batch import run_batch as _batch
        from paperfill.batch import summarize, to_markdown

        rec_dir = Path(recordings_dir) if recordings_dir else record_dir
        recs = (
            [p for p in rec_dir.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz"))]
            if rec_dir.is_dir()
            else []
        )
        if not recs:
            raise ToolError(f"no recordings in {rec_dir}")
        settings = Settings.loads(config_toml) if config_toml else Settings()
        settings.override("run", "strategy", strategy)
        markets = _load_markets(factory, recs)
        out = data_dir / "batch" / strategy
        results = _batch(
            recs,
            markets.get,
            _make_strategy(
                strategy,
                settings.decimal("run", "size"),
                settings.decimal("run", "min_edge"),
                settings.values["strategy"],
            ),
            config=_run_config_from(settings, None),
            out_dir=out,
            log=lambda _m: None,
            workers=workers,
        )
        summary = summarize(results)
        md = to_markdown(results, summary, strategy)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.md").write_text(md, encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return BatchOut(strategy=strategy, out_dir=str(out), summary=summary, markdown=md)

    @mcp.tool(annotations=WRITES_LOCAL)
    def calibrate(
        recordings_dir: str | None = None,
        offsets: Annotated[
            str, Field(description="Seconds after window start, comma-separated.")
        ] = "60,120,180,240",
    ) -> CalibrationOut:
        """Score the fair-value model against the book mid across recordings (Brier)."""
        from paperfill.batch import condition_of
        from paperfill.calibration import run_calibration, save, to_markdown

        rec_dir = Path(recordings_dir) if recordings_dir else record_dir
        recs = (
            [p for p in rec_dir.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz"))]
            if rec_dir.is_dir()
            else []
        )
        if not recs:
            raise ToolError(f"no recordings in {rec_dir}")
        offs = [int(x) for x in offsets.split(",")]
        cal, samples = run_calibration(
            recs,
            lambda cid: _load_market(factory, cid),
            condition_of,
            offsets=offs,
            log=lambda _m: None,
        )
        save(cal, samples, data_dir / "calibration")
        return CalibrationOut(
            windows=cal.windows,
            brier_model={str(k): v for k, v in cal.brier_model.items()},
            brier_mid={str(k): v for k, v in cal.brier_mid.items()},
            markdown=to_markdown(cal),
        )

    @mcp.tool(annotations=WRITES_LOCAL)
    def scan_pairs(recordings_dir: str | None = None) -> PairsOut:
        """Find moments when Up+Down asks cost less than 1 after taker fees, per recording."""
        from paperfill.batch import condition_of
        from paperfill.pairs import scan_recording, summarize, to_markdown

        rec_dir = Path(recordings_dir) if recordings_dir else record_dir
        recs = (
            [p for p in rec_dir.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz"))]
            if rec_dir.is_dir()
            else []
        )
        if not recs:
            raise ToolError(f"no recordings in {rec_dir}")
        markets = _load_markets(factory, recs)
        scans = []
        for p in sorted(recs):
            cid = condition_of(p)
            m = markets.get(cid) if cid else None
            if m is not None:
                scans.append(scan_recording(p, m))
        summary = summarize(scans)
        return PairsOut(
            summary=summary,
            windows=[s.to_json() for s in scans],
            markdown=to_markdown(scans, summary),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=False
        )
    )
    def raise_kill_switch(run_dir: str) -> str:
        """Raise the kill switch of a run directory; the run halts at its next event."""
        d = Path(run_dir)
        if not d.is_dir():
            raise ToolError(f"not a run directory: {run_dir}")
        (d / "KILL").write_text(f"kill requested {datetime.now(UTC).isoformat()}\n")
        return f"kill switch raised: {d / 'KILL'}"

    @mcp.resource("paperfill://runs/{name}/report.md", mime_type="text/markdown")
    def run_report(name: str) -> str:
        """Markdown report of a run directory under data/runs."""
        path = runs_dir / name / "report.md"
        if not path.exists():
            raise ToolError(f"no report for run {name}")
        return path.read_text(encoding="utf-8")

    return mcp


def serve(data_dir: Path) -> None:
    create_server(data_dir=data_dir).run()  # stdio by default (mcp/sdk-run.md)
