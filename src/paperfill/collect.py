"""Record consecutive Up/Down windows for hours, one gzip file per market.

Each cycle picks the soonest open market of the asset and window that still has at
least `min_lead` seconds left, records it until `after_end` seconds past its end, and
moves on. A `STOP` file in the data directory ends the loop after the current window.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from paperfill.markets import MarketInfo, discover
from paperfill.recorder import JsonlSink, record_market, record_path


@dataclass(frozen=True, slots=True)
class Plan:
    market: MarketInfo
    seconds: float


def next_plan(
    source: Any,
    *,
    asset: str,
    window: str,
    now: datetime,
    min_lead: float = 60.0,
    after_end: float = 30.0,
) -> Plan | None:
    """Soonest market with at least `min_lead` seconds to its end, or None."""
    for m in discover(source, asset=asset, window=window, now=now, limit=10):
        if m.end is None:
            continue
        left = (m.end - now).total_seconds()
        if left >= min_lead:
            return Plan(m, left + after_end)
    return None


def collect(
    source_factory: Callable[[], Any],
    async_client_factory: Callable[[], Any],
    *,
    asset: str,
    window: str,
    data_dir: Path,
    hours: float,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Record windows until `hours` elapse or `data_dir / "STOP"` appears. Returns count."""
    import time

    sleep = sleep or time.sleep
    deadline = datetime.now(UTC) + timedelta(hours=hours)
    stop_file = data_dir / "STOP"
    done = 0
    while datetime.now(UTC) < deadline and not stop_file.exists():
        client = source_factory()
        try:
            plan = next_plan(client, asset=asset, window=window, now=datetime.now(UTC))
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        if plan is None:
            log("no market with enough lead; waiting 30 s")
            sleep(30)
            continue
        m = plan.market
        path = record_path(data_dir, m.condition_id, datetime.now(UTC)).with_suffix(".jsonl.gz")
        sink = JsonlSink(path)
        log(f"[{done + 1}] {m.question} -> {path.name} ({plan.seconds:.0f} s)")
        try:
            n = asyncio.run(
                record_market(
                    async_client_factory,
                    [m.yes_token, m.no_token],
                    sink,
                    seconds=plan.seconds,
                    price_symbols=m.price_symbols,
                )
            )
        finally:
            sink.close()
        log(f"    {n} events")
        done += 1
    log(f"collected {done} window(s)" + (" (STOP file)" if stop_file.exists() else ""))
    return done
