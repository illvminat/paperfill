"""Calibration of the fair-value model against realised outcomes across recordings.

For every recording, the model's `p_up` is sampled at fixed offsets from the window
start and compared with the market's payout. Two views come out:

- a reliability table: predictions binned by probability, with the observed frequency
  of "Up" in each bin (a calibrated model has observed ≈ predicted);
- the Brier score per offset, next to the score of the book mid at the same moment,
  so the model is judged against the market's own forecast, not against zero.

Windows are not independent, so these numbers describe this sample.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from paperfill.markets import MarketInfo
from paperfill.runner import BookEvent, PriceEvent, events_from_recording
from paperfill.signals import FairValue


@dataclass(frozen=True, slots=True)
class Sample:
    recording: str
    offset: int
    model_p_up: float | None
    mid_up: float | None
    outcome_up: bool


def sample_window(path: Path, market: MarketInfo, offsets: Iterable[int]) -> list[Sample]:
    """Model and market probabilities of "Up" at each offset (seconds after window start)."""
    if market.window_start is None or market.end is None or market.payouts is None:
        return []
    outcome_up = market.payouts[market.yes_token] == Decimal("1")
    fv = FairValue(market.window_start, market.end)
    pending = sorted(set(offsets))
    mid_up: Decimal | None = None
    out: list[Sample] = []
    for e in events_from_recording(path):
        if isinstance(e, PriceEvent):
            if e.source == "binance":
                fv.on_spot(e.ts, e.value)
            elif e.source == "chainlink.twap":
                fv.on_reference(e.ts, e.value)
        elif (
            isinstance(e, BookEvent)
            and e.book.token_id == market.yes_token
            and e.book.mid is not None
        ):
            mid_up = e.book.mid
        while pending and e.ts >= market.window_start + timedelta(seconds=pending[0]):
            off = pending.pop(0)
            p = fv.p_up(e.ts)
            out.append(
                Sample(path.name, off, p, float(mid_up) if mid_up is not None else None, outcome_up)
            )
    return out


@dataclass(slots=True)
class Calibration:
    offsets: list[int]
    windows: int = 0
    brier_model: dict[int, float | None] = field(default_factory=dict)
    brier_mid: dict[int, float | None] = field(default_factory=dict)
    reliability: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    up_rate: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "offsets": self.offsets,
            "windows": self.windows,
            "up_rate": self.up_rate,
            "brier_model": {str(k): v for k, v in self.brier_model.items()},
            "brier_mid": {str(k): v for k, v in self.brier_mid.items()},
            "reliability": {str(k): v for k, v in self.reliability.items()},
        }


_BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


def _brier(pairs: list[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs)


def calibrate(samples: list[Sample], offsets: list[int]) -> Calibration:
    cal = Calibration(offsets=list(offsets))
    cal.windows = len({s.recording for s in samples})
    outcomes = {s.recording: s.outcome_up for s in samples}
    cal.up_rate = (sum(outcomes.values()) / len(outcomes)) if outcomes else None
    for off in offsets:
        at = [s for s in samples if s.offset == off]
        model = [(s.model_p_up, s.outcome_up) for s in at if s.model_p_up is not None]
        mid = [(s.mid_up, s.outcome_up) for s in at if s.mid_up is not None]
        cal.brier_model[off] = _brier(model)
        cal.brier_mid[off] = _brier(mid)
        rows = []
        for lo, hi in _BINS:
            inb = [(p, y) for p, y in model if lo <= p < hi]
            rows.append(
                {
                    "bin": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "n": len(inb),
                    "predicted": (sum(p for p, _ in inb) / len(inb)) if inb else None,
                    "observed": (sum(1 for _, y in inb if y) / len(inb)) if inb else None,
                }
            )
        cal.reliability[off] = rows
    return cal


def run_calibration(
    recordings: Iterable[Path],
    load_market: Callable[[str], MarketInfo | None],
    condition_of: Callable[[Path], str | None],
    *,
    offsets: list[int],
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> tuple[Calibration, list[Sample]]:
    samples: list[Sample] = []
    for path in sorted(recordings):
        cid = condition_of(path)
        market = load_market(cid) if cid else None
        if market is None or market.payouts is None:
            log(f"skip {path.name}: unresolved or unknown market")
            continue
        samples.extend(sample_window(path, market, offsets))
    return calibrate(samples, offsets), samples


def to_markdown(cal: Calibration) -> str:
    fmt = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
    lines = [
        "# Fair-value calibration",
        "",
        f"Windows: {cal.windows} · observed Up rate: {fmt(cal.up_rate)}",
        "",
        "| Offset (s) | Brier model | Brier book mid |",
        "|---|---|---|",
    ]
    for off in cal.offsets:
        lines.append(f"| {off} | {fmt(cal.brier_model.get(off))} | {fmt(cal.brier_mid.get(off))} |")
    for off in cal.offsets:
        lines += [
            "",
            f"## Reliability at {off} s",
            "",
            "| Predicted bin | n | mean predicted | observed Up |",
            "|---|---|---|---|",
        ]
        for r in cal.reliability.get(off, []):
            lines.append(
                f"| {r['bin']} | {r['n']} | {fmt(r['predicted'])} | {fmt(r['observed'])} |"
            )
    lines += [
        "",
        "Brier score: mean squared error of the probability, 0 is perfect, 0.25 is a coin flip.",
        "A model that beats the book mid at the same moment has information the market lacks;",
        "one that does not is at best redundant. Windows are consecutive and not independent.",
        "",
    ]
    return "\n".join(lines)


def save(cal: Calibration, samples: list[Sample], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "calibration.json").write_text(json.dumps(cal.to_json(), indent=2))
    (out_dir / "calibration.md").write_text(to_markdown(cal))
    with (out_dir / "samples.jsonl").open("w") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")
