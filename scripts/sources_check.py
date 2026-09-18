#!/usr/bin/env python3
"""Warn about stale reference sources for this project.

Reads ~/Documents/refdocs/index.json (a local, non-redistributable store) and prints
every entry tagged with this project that is older than six months. Exit code is
always 0: a stale source is a reason to re-read, not a reason not to build.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT = "paperfill"
STORE = Path.home() / "Documents" / "refdocs" / "index.json"
MAX_AGE = timedelta(days=183)


def main() -> int:
    if not STORE.exists():
        print(f"sources_check: {STORE} not found; skipping (no local store on this machine)")
        return 0
    index = json.loads(STORE.read_text())
    today = date.today()
    stale = 0
    for entry in index.get("sources", []):
        if PROJECT not in entry.get("projects", []):
            continue
        fetched = entry.get("fetched")
        if not fetched:
            print(f"WARN no fetch date: {entry['path']}")
            stale += 1
            continue
        age = today - date.fromisoformat(fetched)
        if age > MAX_AGE:
            print(
                f"WARN {age.days} days old: {entry['path']} ({entry.get('platform_version', '?')})"
            )
            stale += 1
    print(f"sources_check: {stale} stale entr{'y' if stale == 1 else 'ies'} for {PROJECT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
