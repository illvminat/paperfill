"""Command-line entry point.

Subcommands arrive milestone by milestone (docs/zadanie.md), each with its tests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
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
    return parser


def _cmd_discover(args: argparse.Namespace) -> int:
    from polymarket import PublicClient

    from paperfill.markets import discover

    client = PublicClient()
    try:
        rows = list(discover(client, asset=args.asset, window=args.window, limit=args.limit))
    finally:
        client.close()
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


def main(
    argv: Sequence[str] | None = None, *, source_factory: Callable[[], Any] = _default_source
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "discover":
        return _cmd_discover(args, source_factory)
    return 2  # pragma: no cover - argparse rejects unknown commands before we get here


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
