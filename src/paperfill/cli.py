"""Command-line entry point.

Subcommands arrive milestone by milestone (docs/zadanie.md), each with its tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from paperfill import __version__


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"not a decimal number: {text!r}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperfill",
        description="Paper-trading harness for Polymarket. Never places live orders.",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="JSON log level on stderr (DEBUG, INFO, WARNING)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the installed version and exit")

    discover = sub.add_parser(
        "discover", help="list open Up/Down markets with their trading parameters"
    )
    discover.add_argument("--asset", help="asset symbol, e.g. BTC or ETH")
    discover.add_argument("--window", help="market window, e.g. 5m or 15m")
    discover.add_argument("--limit", type=int, default=20, help="max markets to print")

    replay = sub.add_parser(
        "replay", help="fetch the full trade history of a market and replay it as a stream"
    )
    replay.add_argument("--condition", required=True, help="market condition id (0x...)")
    replay.add_argument(
        "--data-dir", type=Path, default=Path("data/history"), help="where history files live"
    )
    replay.add_argument("--refresh", action="store_true", help="re-download even if cached")

    record = sub.add_parser("record", help="record the live order book and trades of a market")
    record.add_argument("--condition", required=True, help="market condition id (0x...)")
    record.add_argument(
        "--seconds", type=float, default=None, help="stop after this many seconds (default: run)"
    )
    record.add_argument(
        "--data-dir", type=Path, default=Path("data/record"), help="where recordings live"
    )
    record.add_argument(
        "--no-prices", action="store_true", help="do not record Binance/Chainlink reference prices"
    )
    record.add_argument(
        "--max-mb", type=float, default=2048.0, help="stop when the file exceeds this many MB"
    )

    run = sub.add_parser("run", help="paper-trade a strategy over a replayed tape or a recording")
    run.add_argument("--condition", required=True, help="market condition id (0x...)")
    run.add_argument(
        "--recording",
        type=Path,
        default=None,
        help="recorder file to run on; without it the trade history is replayed",
    )
    run.add_argument("--history-dir", type=Path, default=Path("data/history"))
    run.add_argument("--runs-dir", type=Path, default=Path("data/runs"))
    run.add_argument("--capital", type=_decimal, default=None)
    run.add_argument("--size", type=_decimal, default=None, help="base quote size, shares")
    run.add_argument(
        "--strategy",
        choices=["two-sided", "fair-value", "taker-probe"],
        default=None,
        help="two-sided: lean by mid drift; fair-value: lean by an external fair value "
        "(needs a recording that includes reference prices)",
    )
    run.add_argument(
        "--min-edge",
        type=_decimal,
        default=None,
        help="fair-value: minimum edge to quote a lone side",
    )
    run.add_argument("--max-order", type=_decimal, default=None)
    run.add_argument("--max-position", type=_decimal, default=None)
    run.add_argument("--max-market", type=_decimal, default=None)
    run.add_argument("--max-exposure", type=_decimal, default=None)
    run.add_argument("--daily-loss", type=_decimal, default=None)
    run.add_argument("--total-loss", type=_decimal, default=None)
    run.add_argument("--config", type=Path, default=None, help="TOML settings file (flags win)")
    run.add_argument(
        "--resume",
        type=Path,
        default=None,
        metavar="RUN_DIR",
        help="continue a halted or interrupted run from its journal (remove its KILL file first)",
    )

    report = sub.add_parser("report", help="rebuild the report of a run from its journal")
    report.add_argument("run_dir", type=Path)

    journal = sub.add_parser("journal", help="journal tools")
    jsub = journal.add_subparsers(dest="journal_command", required=True)
    jverify = jsub.add_parser("verify", help="verify the hash chain of a journal file")
    jverify.add_argument("path", type=Path)

    kill = sub.add_parser("kill", help="raise the kill switch of a run directory")
    kill.add_argument("run_dir", type=Path)

    collect = sub.add_parser("collect", help="record consecutive windows for hours (gzip files)")
    collect.add_argument("--asset", default="BTC")
    collect.add_argument("--window", default="5m")
    collect.add_argument("--hours", type=float, default=4.0)
    collect.add_argument("--data-dir", type=Path, default=Path("data/record"))

    batch = sub.add_parser("batch", help="run a strategy over every recording and summarise")
    batch.add_argument("--recordings", type=Path, default=Path("data/record"), help="directory")
    batch.add_argument("--out", type=Path, default=Path("data/batch"))
    batch.add_argument(
        "--strategy", choices=["two-sided", "fair-value", "taker-probe"], default=None
    )
    batch.add_argument("--capital", type=_decimal, default=None)
    batch.add_argument("--size", type=_decimal, default=None)
    batch.add_argument("--min-edge", type=_decimal, default=None)
    batch.add_argument("--workers", type=int, default=1, help="parallel processes")
    batch.add_argument("--config", type=Path, default=None, help="TOML settings file (flags win)")

    sweep = sub.add_parser("sweep", help="parameter grid on earlier windows, judged on later ones")
    sweep.add_argument("--recordings", type=Path, default=Path("data/record"))
    sweep.add_argument("--out", type=Path, default=Path("data/sweep"))
    sweep.add_argument("--strategy", choices=["two-sided", "fair-value"], default="fair-value")
    sweep.add_argument("--size", default="5")
    sweep.add_argument("--train-share", type=float, default=0.6)
    sweep.add_argument("--workers", type=int, default=1)
    sweep.add_argument(
        "--grid",
        type=Path,
        default=None,
        help="JSON file {param: [values]}; default grid otherwise",
    )
    sweep.add_argument(
        "--config", type=Path, default=None, help="TOML settings file for capital, risk, model"
    )

    cal = sub.add_parser("calibrate", help="calibrate the fair-value model across recordings")
    cal.add_argument("--recordings", type=Path, default=Path("data/record"))
    cal.add_argument("--out", type=Path, default=Path("data/calibration"))
    cal.add_argument("--offsets", default="30,60,120,180,240", help="seconds after window start")

    mcp = sub.add_parser("mcp", help="serve paperfill as an MCP server over stdio")
    mcp.add_argument("--data-dir", type=Path, default=Path("data"))

    dash = sub.add_parser("dashboard", help="serve the dashboard of a run directory")
    dash.add_argument("run_dir", type=Path)
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8765)
    return parser


def _default_source() -> Any:
    from polymarket import PublicClient

    return PublicClient()


def _cmd_discover(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.markets import discover

    client = source_factory()
    try:
        rows = list(discover(client, asset=args.asset, window=args.window, limit=args.limit))
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    now = datetime.now(UTC)
    print(
        f"{'asset':<6} {'win':<4} {'ends (UTC)':<20} {'in':>6} {'min':>4} "
        f"{'tick':>5} {'fee':>5} {'rebate':>6}  condition_id"
    )
    for m in rows:
        secs = int((m.end - now).total_seconds()) if m.end else 0
        print(
            f"{m.asset or '?':<6} {m.window or '?':<4} "
            f"{m.end.strftime('%Y-%m-%d %H:%M:%S') if m.end else '?':<20} "
            f"{secs:>5}s {m.min_order_size:>4} {m.tick_size:>5} "
            f"{m.fees.rate:>5} {m.fees.rebate_rate:>6}  {m.condition_id}"
        )
    print(f"{len(rows)} market(s)")
    return 0


def _cmd_replay(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.history import (
        RateLimited,
        fetch_trades,
        history_path,
        load_trades,
        replay,
        save_trades,
        stream_digest,
    )

    path = history_path(args.data_dir, args.condition)
    if path.exists() and not args.refresh:
        header, trades = load_trades(path)
        print(f"cached: {path} (fetched {header['fetched_at']})")
    else:
        client = source_factory()

        def report(ev: RateLimited) -> None:
            print(f"rate limited: waiting {ev.retry_after}s (attempt {ev.attempt})")

        try:
            trades = fetch_trades(client, args.condition, on_event=report)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        save_trades(path, args.condition, trades, fetched_at=datetime.now(UTC))
        print(f"saved: {path}")
    stream = list(replay(trades))
    print(f"{len(stream)} trades")
    if stream:
        print(f"first: {stream[0].ts.isoformat()}  last: {stream[-1].ts.isoformat()}")
        by_token: dict[str, tuple[Decimal, Decimal]] = {}
        for t in stream:
            notional, size = by_token.get(t.token_id, (Decimal(0), Decimal(0)))
            by_token[t.token_id] = (notional + t.price * t.size, size + t.size)
        for token, (notional, size) in by_token.items():
            label = next((t.outcome for t in stream if t.token_id == token), None) or "?"
            vwap = (notional / size).quantize(Decimal("0.0001")) if size else Decimal(0)
            print(f"  {label:<5} size {size}  vwap {vwap}")
    print(f"digest: {stream_digest(stream)}")
    return 0


def _cmd_record(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    import asyncio

    from paperfill.markets import from_sdk
    from paperfill.recorder import JsonlSink, record_market, record_path

    client = source_factory()
    try:
        page = client.list_markets(condition_ids=[args.condition], page_size=1).first_page()
        if not page.items:
            print(f"no market with condition id {args.condition}")
            return 1
        market = from_sdk(page.items[0])
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    started = datetime.now(UTC)
    path = record_path(args.data_dir, args.condition, started)
    sink = JsonlSink(path)
    print(f"recording {market.question} -> {path}")

    def async_client() -> Any:
        from polymarket import AsyncPublicClient

        return AsyncPublicClient()

    try:
        n = asyncio.run(
            record_market(
                async_client,
                [market.yes_token, market.no_token],
                sink,
                seconds=args.seconds,
                price_symbols=None if args.no_prices else market.price_symbols,
                max_bytes=int(args.max_mb * 1_000_000),
            )
        )
    finally:
        sink.close()
    print(f"{n} events")
    return 0


def _load_market(source_factory: Callable[[], Any], condition_id: str) -> Any:
    from paperfill.markets import from_sdk

    client = source_factory()
    try:
        for closed in (False, True):
            page = client.list_markets(
                condition_ids=[condition_id], closed=closed, page_size=1
            ).first_page()
            if page.items:
                return from_sdk(page.items[0])
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    return None


def _make_strategy(
    name: str, size: Decimal, min_edge: Decimal, extra: dict[str, Any] | None = None
) -> Callable[[], Any]:
    """Picklable factory for a strategy by name; `extra` is the [strategy] config section."""
    from datetime import timedelta
    from functools import partial

    from paperfill.strategy import FairValueQuoter, TakerProbe, TwoSidedQuoter

    x = extra or {}
    if name == "fair-value":
        stop = int(x.get("stop_after_s", 0) or 0)
        return partial(
            FairValueQuoter,
            size=size,
            min_edge=min_edge,
            edge_gain=Decimal(str(x.get("edge_gain", "10"))),
            shrink_to_mid=Decimal(str(x.get("shrink_to_mid", "0"))),
            stop_after=timedelta(seconds=stop) if stop else None,
        )
    if name == "taker-probe":
        return partial(TakerProbe, size=size)
    return partial(
        TwoSidedQuoter,
        size=size,
        lookback=timedelta(seconds=int(x.get("lookback_s", 30))),
        lean_gain=Decimal(str(x.get("lean_gain", "20"))),
    )


def _settings_from(args: argparse.Namespace) -> Any:
    """Settings = defaults < TOML file (--config) < explicit flags."""
    from paperfill.config import Settings

    settings = Settings.load(getattr(args, "config", None))
    for key in ("capital", "size", "strategy", "min_edge"):
        settings.override("run", key, getattr(args, key, None))
    risk_keys = (
        "max_order",
        "max_position",
        "max_market",
        "max_exposure",
        "daily_loss",
        "total_loss",
    )
    for key in risk_keys:
        settings.override("risk", key, getattr(args, key, None))
    return settings


def _run_config_from(settings: Any, kill_file: Path | None) -> Any:
    from paperfill.risk import RiskLimits
    from paperfill.runner import RunConfig

    limits = RiskLimits(
        max_order_notional=settings.decimal("risk", "max_order"),
        max_position_shares=settings.decimal("risk", "max_position"),
        max_market_notional=settings.decimal("risk", "max_market"),
        max_total_exposure=settings.decimal("risk", "max_exposure"),
        daily_loss_limit=settings.decimal("risk", "daily_loss"),
        total_loss_limit=settings.decimal("risk", "total_loss"),
    )
    return RunConfig(
        capital=settings.decimal("run", "capital"),
        limits=limits,
        kill_file=kill_file,
        vol_sample_seconds=float(settings.get("model", "vol_sample_seconds")),
        vol_halflife_seconds=float(settings.get("model", "vol_halflife_seconds")),
    )


def _strategy_factory(settings: Any) -> Callable[[], Any]:
    return _make_strategy(
        settings.get("run", "strategy"),
        settings.decimal("run", "size"),
        settings.decimal("run", "min_edge"),
        settings.values["strategy"],
    )


def _cmd_collect(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.collect import collect

    def async_client() -> Any:
        from polymarket import AsyncPublicClient

        return AsyncPublicClient()

    n = collect(
        source_factory,
        async_client,
        asset=args.asset,
        window=args.window,
        data_dir=args.data_dir,
        hours=args.hours,
    )
    return 0 if n else 1


def _recordings_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz"))]


def _load_markets(source_factory: Callable[[], Any], recordings: list[Path]) -> dict[str, Any]:
    """Resolve every recording's market once, up front (picklable dict for workers)."""
    from paperfill.batch import condition_of

    out: dict[str, Any] = {}
    for p in recordings:
        cid = condition_of(p)
        if cid and cid not in out:
            m = _load_market(source_factory, cid)
            if m is not None:
                out[cid] = m
    return out


def _cmd_sweep(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.sweep import DEFAULT_GRIDS, sweep, to_markdown

    recs = _recordings_in(args.recordings)
    if not recs:
        print(f"no recordings (*.jsonl, *.jsonl.gz) in {args.recordings}")
        return 1
    grid = json.loads(args.grid.read_text()) if args.grid else DEFAULT_GRIDS[args.strategy]
    markets = _load_markets(source_factory, recs)
    result = sweep(
        recs,
        markets.get,
        strategy=args.strategy,
        grid=grid,
        size=args.size,
        base_config=_run_config_from(_settings_from(args), None),
        out_dir=args.out / args.strategy,
        train_share=args.train_share,
        workers=args.workers,
    )
    print(to_markdown(result))
    return 0


def _cmd_batch(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.batch import run_batch, summarize, to_markdown

    recs = _recordings_in(args.recordings)
    if not recs:
        print(f"no recordings (*.jsonl, *.jsonl.gz) in {args.recordings}")
        return 1
    settings = _settings_from(args)
    if (
        settings.get("run", "strategy") == "two-sided"
        and args.strategy is None
        and args.config is None
    ):
        settings.values["run"]["strategy"] = "fair-value"  # batch's historical default
    name = settings.get("run", "strategy")
    out = args.out / name
    markets = _load_markets(source_factory, recs)
    results = run_batch(
        recs,
        markets.get,
        _strategy_factory(settings),
        config=_run_config_from(settings, None),
        out_dir=out,
        workers=args.workers,
    )
    summary = summarize(results)
    md = to_markdown(results, summary, name)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(md, encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(md)
    return 0


def _cmd_calibrate(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.batch import condition_of
    from paperfill.calibration import run_calibration, save, to_markdown

    recs = _recordings_in(args.recordings)
    if not recs:
        print(f"no recordings (*.jsonl, *.jsonl.gz) in {args.recordings}")
        return 1
    offsets = [int(x) for x in args.offsets.split(",")]
    cal, samples = run_calibration(
        recs, lambda cid: _load_market(source_factory, cid), condition_of, offsets=offsets
    )
    save(cal, samples, args.out)
    print(to_markdown(cal))
    return 0


def _cmd_run(args: argparse.Namespace, source_factory: Callable[[], Any]) -> int:
    from paperfill.history import fetch_trades, history_path, load_trades, replay, save_trades
    from paperfill.journal import Journal
    from paperfill.report import compute, to_json, to_markdown
    from paperfill.runner import (
        Runner,
        events_from_recording,
        recording_covers_resolution,
    )

    market = _load_market(source_factory, args.condition)
    if market is None:
        print(f"no market with condition id {args.condition}")
        return 1
    payouts = market.payouts
    if args.recording is not None:
        mode, events = "record", events_from_recording(args.recording)
        if not recording_covers_resolution(args.recording, market.end):
            payouts = None  # partial tape: mark to market, never settle at the final outcome
    else:
        mode = "replay"
        path = history_path(args.history_dir, args.condition)
        if path.exists():
            _, trades = load_trades(path)
        else:
            client = source_factory()
            try:
                trades = fetch_trades(client, args.condition)
            finally:
                close = getattr(client, "close", None)
                if close:
                    close()
            save_trades(path, args.condition, trades, fetched_at=datetime.now(UTC))
        events = replay(trades)
    settings = _settings_from(args)
    if args.resume is not None:
        run_dir = args.resume
        if (run_dir / "KILL").exists():
            print(f"refusing to resume: {run_dir / 'KILL'} is present; remove it first")
            return 1
        if not (run_dir / "journal.jsonl").exists():
            print(f"nothing to resume in {run_dir}")
            return 1
    else:
        started = datetime.now(UTC)
        run_dir = args.runs_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{args.condition[:10]}"
        run_dir.mkdir(parents=True, exist_ok=True)
    config = _run_config_from(settings, run_dir / "KILL")
    journal = Journal.open(run_dir / "journal.jsonl")
    strategy: Any = _strategy_factory(settings)()
    print(f"run: {market.question} [{mode}] -> {run_dir}" + ("  (resumed)" if args.resume else ""))
    try:
        runner = Runner(
            market, strategy, journal, config, mode=mode, resume=args.resume is not None
        )
        result = runner.run(events, payouts=payouts)
    finally:
        journal.close()
    metrics = compute(journal.entries)
    (run_dir / "report.md").write_text(to_markdown(metrics), encoding="utf-8")
    (run_dir / "report.json").write_text(to_json(metrics), encoding="utf-8")
    print(
        f"events {result.events}  fills {len(result.fills)}  "
        f"realized P&L {result.portfolio.realized_pnl}  fees {result.portfolio.fees_paid}  "
        f"settled {'yes' if result.settled else 'no'}"
        + (f"  HALTED: {result.halted}" if result.halted else "")
    )
    print(f"report: {run_dir / 'report.md'}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from paperfill.journal import read_entries, verify
    from paperfill.report import compute, to_json, to_markdown

    entries = read_entries(args.run_dir / "journal.jsonl")
    v = verify(entries)
    if not v.ok:
        print(f"journal FAILED verification at seq {v.first_bad_seq}: {v.reason}")
        return 1
    metrics = compute(entries)
    (args.run_dir / "report.md").write_text(to_markdown(metrics), encoding="utf-8")
    (args.run_dir / "report.json").write_text(to_json(metrics), encoding="utf-8")
    print(to_markdown(metrics))
    return 0


def _cmd_journal_verify(args: argparse.Namespace) -> int:
    from paperfill.journal import verify_file

    v = verify_file(args.path)
    if v.ok:
        print(f"OK: {v.entries} entries, chain intact")
        return 0
    print(f"FAILED at seq {v.first_bad_seq}: {v.reason} ({v.entries} entries read)")
    return 1


def _cmd_kill(args: argparse.Namespace) -> int:
    path = args.run_dir / "KILL"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"kill requested {datetime.now(UTC).isoformat()}\n")
    print(f"kill switch raised: {path}")
    return 0


def main(
    argv: Sequence[str] | None = None, *, source_factory: Callable[[], Any] = _default_source
) -> int:
    args = build_parser().parse_args(argv)
    from paperfill.log import setup as setup_logging

    setup_logging(args.log_level)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "discover":
        return _cmd_discover(args, source_factory)
    if args.command == "replay":
        return _cmd_replay(args, source_factory)
    if args.command == "record":
        return _cmd_record(args, source_factory)
    if args.command == "run":
        return _cmd_run(args, source_factory)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "journal":
        return _cmd_journal_verify(args)
    if args.command == "kill":
        return _cmd_kill(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args, source_factory)
    if args.command == "sweep":
        return _cmd_sweep(args, source_factory)
    if args.command == "collect":
        return _cmd_collect(args, source_factory)
    if args.command == "batch":
        return _cmd_batch(args, source_factory)
    if args.command == "mcp":
        try:
            from paperfill.mcp_server import serve as serve_mcp
        except ImportError:
            print(
                "the MCP server needs the extra: uv sync --extra mcp "
                "(or pip install 'paperfill[mcp]')"
            )
            return 1
        serve_mcp(args.data_dir)
        return 0
    if args.command == "dashboard":
        from paperfill.dashboard import serve

        print(f"dashboard: http://{args.host}:{args.port}/  (run dir {args.run_dir})")
        serve(args.run_dir, args.host, args.port)
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands before we get here


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
