"""Command-line entry point.

Subcommands arrive milestone by milestone (docs/zadanie.md), each with its tests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfill import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperfill",
        description="Paper-trading harness for Polymarket. Never places live orders.",
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
                async_client, [market.yes_token, market.no_token], sink, seconds=args.seconds
            )
        )
    finally:
        sink.close()
    print(f"{n} events")
    return 0


def main(
    argv: Sequence[str] | None = None, *, source_factory: Callable[[], Any] = _default_source
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "discover":
        return _cmd_discover(args, source_factory)
    if args.command == "replay":
        return _cmd_replay(args, source_factory)
    if args.command == "record":
        return _cmd_record(args, source_factory)
    return 2  # pragma: no cover - argparse rejects unknown commands before we get here


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
