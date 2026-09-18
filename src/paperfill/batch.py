"""Run one strategy over many recordings and aggregate the settled results.

This is the first half of a parameter sweep (docs/zadanie.md, I3): the same strategy
and parameters over every window on disk, one journal per window, one table at the
end. Markets that are not resolved yet are run but reported as unsettled and left out
of the P&L totals.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfill.journal import Journal
from paperfill.markets import MarketInfo
from paperfill.report import Metrics, compute
from paperfill.runner import RunConfig, Runner, events_from_recording, recording_covers_resolution

_NAME = re.compile(r"^(?P<cid>0x[0-9a-f]{64})-(?P<ts>\d{8}T\d{6}Z)\.jsonl(\.gz)?$")
ZERO = Decimal("0")


def condition_of(path: Path) -> str | None:
    m = _NAME.match(path.name)
    return m["cid"] if m else None


@dataclass(frozen=True, slots=True)
class WindowResult:
    recording: str
    question: str
    settled: bool
    metrics: Metrics


def run_batch(
    recordings: Iterable[Path],
    load_market: Callable[[str], MarketInfo | None],
    make_strategy: Callable[[], Any],
    *,
    config: RunConfig,
    out_dir: Path,
    log: Callable[[str], None] = print,
) -> list[WindowResult]:
    results: list[WindowResult] = []
    for path in sorted(recordings):
        cid = condition_of(path)
        if cid is None:
            log(f"skip {path.name}: not a recording name")
            continue
        market = load_market(cid)
        if market is None:
            log(f"skip {path.name}: market not found")
            continue
        payouts = market.payouts if recording_covers_resolution(path, market.end) else None
        run_dir = out_dir / path.name.split(".")[0]
        run_dir.mkdir(parents=True, exist_ok=True)
        journal = Journal.open(run_dir / "journal.jsonl")
        strategy = make_strategy()
        try:
            Runner(market, strategy, journal, config, mode="record").run(
                events_from_recording(path), payouts=payouts
            )
        finally:
            journal.close()
        metrics = compute(journal.entries)
        (run_dir / "report.json").write_text(
            json.dumps(metrics.to_json(), indent=2, sort_keys=True)
        )
        results.append(WindowResult(path.name, market.question, metrics.settled, metrics))
        log(
            f"{market.question}: fills {metrics.fills}, fees {metrics.fees}, "
            f"P&L {metrics.realized_pnl if metrics.settled else 'unsettled'}"
        )
    return results


def summarize(results: list[WindowResult]) -> dict[str, Any]:
    settled = [r for r in results if r.settled]
    pnls = [r.metrics.realized_pnl for r in settled]
    wins = sum(1 for p in pnls if p > ZERO)
    total = sum(pnls, ZERO)
    fees = sum((r.metrics.fees for r in settled), ZERO)
    return {
        "windows": len(results),
        "settled": len(settled),
        "unsettled": len(results) - len(settled),
        "winning_windows": wins,
        "losing_windows": sum(1 for p in pnls if p < ZERO),
        "total_pnl": str(total),
        "mean_pnl": str((total / len(pnls)).quantize(Decimal("0.0001"))) if pnls else None,
        "worst_window": str(min(pnls)) if pnls else None,
        "best_window": str(max(pnls)) if pnls else None,
        "total_fees": str(fees),
        "fills": sum(r.metrics.fills for r in results),
    }


def to_markdown(results: list[WindowResult], summary: dict[str, Any], strategy: str) -> str:
    lines = [
        f"# paperfill batch: {strategy}",
        "",
        "| Window | Fills | Fees | Settled | P&L |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        m = r.metrics
        lines.append(
            f"| {r.question} | {m.fills} | {m.fees} | {'yes' if r.settled else 'no'} "
            f"| {m.realized_pnl if r.settled else '—'} |"
        )
    lines += ["", "| Summary | |", "|---|---|"]
    for k, v in summary.items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "One strategy, fixed parameters, every window on disk. Windows are not independent",
        "(same asset, same hours); a mean over a few dozen of them is a description of this",
        "sample, not an expectation.",
        "",
    ]
    return "\n".join(lines)
