"""paperfill: a paper-trading harness for Polymarket prediction markets.

The package never places live orders. See README.md, section "What this is not".
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("paperfill")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare checkout
    __version__ = "0+unknown"

__all__ = ["__version__"]
