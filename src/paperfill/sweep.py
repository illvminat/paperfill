"""Parameter sweep with a time split: choose on earlier windows, judge on later ones.

Every combination of the grid is run on the training windows; the best by mean settled
P&L is then run once on the test windows. Reporting the test result of the chosen
combination, and only that, is what keeps the sweep from being a curve fit.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from paperfill.batch import run_batch, split_by_time, summarize
from paperfill.markets import MarketInfo
from paperfill.runner import RunConfig
from paperfill.strategy import FairValueQuoter, TwoSidedQuoter


def _fair_value(size, min_edge, edge_gain, shrink_to_mid, stop_after_s):
    return FairValueQuoter(
        size=Decimal(size),
        min_edge=Decimal(min_edge),
        edge_gain=Decimal(edge_gain),
        shrink_to_mid=Decimal(shrink_to_mid),
        stop_after=timedelta(seconds=stop_after_s) if stop_after_s else None,
    )


def _two_sided(size, lookback_s, lean_gain):
    return TwoSidedQuoter(
        size=Decimal(size), lookback=timedelta(seconds=lookback_s), lean_gain=Decimal(lean_gain)
    )


DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "fair-value": {
        "min_edge": ["0.02", "0.05"],
        "edge_gain": ["10", "5"],
        "shrink_to_mid": ["0", "0.5"],
        "stop_after_s": [0, 120],
        "vol_sample_seconds": [1.0, 60.0],
    },
    "two-sided": {"lookback_s": [30, 120], "lean_gain": ["20", "5"]},
}


@dataclass(frozen=True, slots=True)
class Combo:
    strategy: str
    params: dict[str, Any]

    @property
    def key(self) -> str:
        return ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))


def combos(strategy: str, grid: dict[str, list[Any]]) -> list[Combo]:
    keys = sorted(grid)
    return [
        Combo(strategy, dict(zip(keys, vals, strict=True)))
        for vals in itertools.product(*(grid[k] for k in keys))
    ]


def factory(combo: Combo, size: str) -> Callable[[], Any]:
    p = combo.params
    if combo.strategy == "fair-value":
        return partial(
            _fair_value, size, p["min_edge"], p["edge_gain"], p["shrink_to_mid"], p["stop_after_s"]
        )
    return partial(_two_sided, size, p["lookback_s"], p["lean_gain"])


def config_for(combo: Combo, base: RunConfig) -> RunConfig:
    if "vol_sample_seconds" in combo.params:
        return replace(base, vol_sample_seconds=float(combo.params["vol_sample_seconds"]))
    return base


def sweep(
    recordings: Iterable[Path],
    load_market: Callable[[str], MarketInfo | None],
    *,
    strategy: str,
    grid: dict[str, list[Any]],
    size: str,
    base_config: RunConfig,
    out_dir: Path,
    train_share: float = 0.6,
    workers: int = 1,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> dict[str, Any]:
    train, test = split_by_time(list(recordings), train_share)
    log(
        f"train {len(train)} windows, test {len(test)} windows, "
        f"{len(combos(strategy, grid))} combinations"
    )
    rows = []
    for combo in combos(strategy, grid):
        res = run_batch(
            train,
            load_market,
            factory(combo, size),
            config=config_for(combo, base_config),
            out_dir=out_dir / "train" / combo.key.replace(",", "_").replace("=", "-"),
            log=lambda _m: None,
            workers=workers,
        )
        s = summarize(res)
        rows.append({"combo": combo.key, "params": combo.params, **s})
        log(f"  {combo.key}: mean {s['mean_pnl']} over {s['settled']} settled windows")
    ranked = sorted(
        (r for r in rows if r["mean_pnl"] is not None),
        key=lambda r: Decimal(r["mean_pnl"]),
        reverse=True,
    )
    best = ranked[0] if ranked else None
    test_summary = None
    if best is not None and test:
        combo = Combo(strategy, best["params"])
        res = run_batch(
            test,
            load_market,
            factory(combo, size),
            config=config_for(combo, base_config),
            out_dir=out_dir / "test",
            log=lambda _m: None,
            workers=workers,
        )
        test_summary = summarize(res)
        log(
            f"best on train: {best['combo']} -> test mean {test_summary['mean_pnl']} "
            f"over {test_summary['settled']} windows"
        )
    result = {
        "strategy": strategy,
        "size": size,
        "train_windows": len(train),
        "test_windows": len(test),
        "grid": grid,
        "train": rows,
        "best": best,
        "test": test_summary,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sweep.json").write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "sweep.md").write_text(to_markdown(result))
    return result


def to_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# paperfill sweep: {r['strategy']}",
        "",
        f"Train windows: {r['train_windows']} (earlier) · "
        f"test windows: {r['test_windows']} (later) · size {r['size']}",
        "",
        "| Combination | Settled | Win / lose | Mean P&L | Worst | Best |",
        "|---|---|---|---|---|---|",
    ]
    for row in sorted(r["train"], key=lambda x: Decimal(x["mean_pnl"] or 0), reverse=True):
        lines.append(
            f"| {row['combo']} | {row['settled']} | "
            f"{row['winning_windows']} / {row['losing_windows']} | {row['mean_pnl']} | "
            f"{row['worst_window']} | {row['best_window']} |"
        )
    if r["best"] and r["test"]:
        t = r["test"]
        lines += [
            "",
            f"**Chosen on train:** `{r['best']['combo']}` (train mean {r['best']['mean_pnl']})",
            "",
            f"**On test:** mean {t['mean_pnl']} over {t['settled']} windows, "
            f"{t['winning_windows']} / {t['losing_windows']} win / lose, "
            f"worst {t['worst_window']}, best {t['best_window']}.",
        ]
    lines += [
        "",
        "The test figure is the only one that counts. "
        "Train figures are what the grid was chosen on.",
        "",
    ]
    return "\n".join(lines)
