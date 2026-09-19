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


def run_window(
    path: Path,
    market: MarketInfo,
    make_strategy: Callable[[], Any],
    config: RunConfig,
    out_dir: Path,
) -> WindowResult:
    """One recording, one journal, one report; safe to call from a worker process."""
    payouts = market.payouts if recording_covers_resolution(path, market.end) else None
    run_dir = out_dir / path.name.split(".")[0]
    run_dir.mkdir(parents=True, exist_ok=True)
    journal_path = run_dir / "journal.jsonl"
    if journal_path.exists():
        journal_path.unlink()  # a rerun replaces the previous journal of this window
    journal = Journal.open(journal_path)
    try:
        Runner(market, make_strategy(), journal, config, mode="record").run(
            events_from_recording(path), payouts=payouts
        )
    finally:
        journal.close()
    metrics = compute(journal.entries)
    (run_dir / "report.json").write_text(json.dumps(metrics.to_json(), indent=2, sort_keys=True))
    return WindowResult(path.name, market.question, metrics.settled, metrics)


def run_batch(
    recordings: Iterable[Path],
    load_market: Callable[[str], MarketInfo | None],
    make_strategy: Callable[[], Any],
    *,
    config: RunConfig,
    out_dir: Path,
    log: Callable[[str], None] = print,
    workers: int = 1,
) -> list[WindowResult]:
    """Run every recording; with `workers > 1` windows run in separate processes.

    `make_strategy` and `config` must be picklable for that: module-level factories or
    `functools.partial` over module-level classes, never lambdas.
    """
    jobs: list[tuple[Path, MarketInfo]] = []
    for path in sorted(recordings):
        cid = condition_of(path)
        if cid is None:
            log(f"skip {path.name}: not a recording name")
            continue
        market = load_market(cid)
        if market is None:
            log(f"skip {path.name}: market not found")
            continue
        jobs.append((path, market))
    results: list[WindowResult] = []
    if workers <= 1:
        for path, market in jobs:
            results.append(run_window(path, market, make_strategy, config, out_dir))
            log(_line(results[-1]))
        return results
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_window, p, m, make_strategy, config, out_dir) for p, m in jobs]
        for fut in futures:
            results.append(fut.result())
            log(_line(results[-1]))
    return results


def _line(r: WindowResult) -> str:
    m = r.metrics
    return (
        f"{r.question}: fills {m.fills}, fees {m.fees}, "
        f"P&L {m.realized_pnl if r.settled else 'unsettled'}"
    )


def split_by_time(
    recordings: Iterable[Path], train_share: float = 0.6
) -> tuple[list[Path], list[Path]]:
    """Earlier windows train, later windows test; never the other way round."""
    ordered = sorted(recordings, key=lambda p: p.name.split("-")[-1])
    k = round(len(ordered) * train_share)
    return ordered[:k], ordered[k:]


def _bootstrap_ci(
    values: list[Decimal], *, n: int = 2000, seed: int = 7
) -> tuple[Decimal, Decimal]:
    """95% percentile bootstrap of the mean; deterministic seed so reports reproduce."""
    import random

    rng = random.Random(seed)
    xs = [float(v) for v in values]
    means = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    lo, hi = means[int(0.025 * n)], means[int(0.975 * n) - 1]
    return Decimal(f"{lo:.4f}"), Decimal(f"{hi:.4f}")


def summarize(results: list[WindowResult]) -> dict[str, Any]:
    """Settled windows carry realized P&L; unsettled ones are listed, not averaged.

    Halted windows are settled like any other (a breaker halt is an outcome), so they
    are inside every figure; their count is shown so nobody has to guess.
    """
    import math

    settled = [r for r in results if r.settled]
    pnls = [r.metrics.realized_pnl for r in settled]
    wins = sum(1 for p in pnls if p > ZERO)
    total = sum(pnls, ZERO)
    fees = sum((r.metrics.fees for r in settled), ZERO)
    mean = (total / len(pnls)) if pnls else None
    if mean is not None and len(pnls) >= 2:
        fm = float(mean)
        var = sum((float(p) - fm) ** 2 for p in pnls) / (len(pnls) - 1)
        sd = math.sqrt(var)
        t_stat = fm / (sd / math.sqrt(len(pnls))) if sd > 0 else None
        lo, hi = _bootstrap_ci(pnls)
    else:
        sd, t_stat, lo, hi = None, None, None, None
    return {
        "windows": len(results),
        "settled": len(settled),
        "unsettled": len(results) - len(settled),
        "halted": sum(1 for r in results if r.metrics.halted),
        "winning_windows": wins,
        "losing_windows": sum(1 for p in pnls if p < ZERO),
        "total_pnl": str(total),
        "mean_pnl": str(mean.quantize(Decimal("0.0001"))) if mean is not None else None,
        "mean_ci95": [str(lo), str(hi)] if lo is not None else None,
        "t_stat": round(t_stat, 2) if t_stat is not None else None,
        "stdev": f"{sd:.4f}" if sd is not None else None,
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
        "One strategy, fixed parameters, every window on disk. Halted windows are settled",
        "and included. `mean_ci95` is a percentile bootstrap of the mean; `t_stat` is the",
        "mean over its standard error. Windows are consecutive and not independent, so",
        "both understate the uncertainty; treat a |t| under 2 as noise.",
        "",
    ]
    return "\n".join(lines)
