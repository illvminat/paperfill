"""Run settings from a TOML file, overridden by explicit command-line flags.

    [run]        capital = "100"   size = "5"   strategy = "fair-value"   min_edge = "0.02"
    [risk]       max_order = "50"  max_position = "100"  max_market = "100"
                 max_exposure = "200"  daily_loss = "20"  total_loss = "50"
    [model]      vol_sample_seconds = 0.0  vol_halflife_seconds = 60.0
    [strategy]   edge_gain = "10"  shrink_to_mid = "0"  stop_after_s = 0
                 lookback_s = 30  lean_gain = "20"

Precedence: a flag given on the command line, then the file, then the default. Money
values are strings in the file so they reach `Decimal` untouched.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "run": {"capital": "100", "size": "5", "strategy": "two-sided", "min_edge": "0.02"},
    "risk": {
        "max_order": "50",
        "max_position": "100",
        "max_market": "100",
        "max_exposure": "200",
        "daily_loss": "20",
        "total_loss": "50",
    },
    "model": {"vol_sample_seconds": 0.0, "vol_halflife_seconds": 60.0},
    "strategy": {
        "edge_gain": "10",
        "shrink_to_mid": "0",
        "stop_after_s": 0,
        "lookback_s": 30,
        "lean_gain": "20",
    },
}


@dataclass(slots=True)
class Settings:
    values: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULTS.items()}
    )
    source: str = "defaults"

    @classmethod
    def load(cls, path: Path | None) -> Settings:
        s = cls()
        if path is None:
            return s
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for section, keys in data.items():
            if section not in s.values:
                raise ValueError(f"{path}: unknown section [{section}]")
            for key, value in keys.items():
                if key not in s.values[section]:
                    raise ValueError(f"{path}: unknown key {key} in [{section}]")
                s.values[section][key] = value
        s.source = str(path)
        return s

    def override(self, section: str, key: str, value: Any) -> None:
        """A command-line flag beats the file; `None` means the flag was not given."""
        if value is not None:
            self.values[section][key] = value

    def decimal(self, section: str, key: str) -> Decimal:
        return Decimal(str(self.values[section][key]))

    def get(self, section: str, key: str) -> Any:
        return self.values[section][key]
