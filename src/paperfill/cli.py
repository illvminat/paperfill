"""Command-line entry point.

Only `paperfill version` exists at this stage; subcommands are added milestone by
milestone (docs/zadanie.md) and each one arrives together with its tests.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from paperfill import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperfill",
        description="Paper-trading harness for Polymarket. Never places live orders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the installed version and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands before we get here


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
