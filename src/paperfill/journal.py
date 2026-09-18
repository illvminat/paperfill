"""Append-only decision journal with a hash chain.

Every order, fill, rejection, risk decision and lifecycle event is one entry:

    {seq, ts, kind, data, prev, hash}

`hash` is SHA-256 over the canonical JSON of (seq, ts, kind, data, prev). Editing or
deleting any line breaks every hash after it, which `verify` reports with the first
bad sequence number. Silence is forbidden (CLAUDE.md): nothing is dropped or
downgraded on the way in.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # enums
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def entry_hash(seq: int, ts: str, kind: str, data: dict[str, Any], prev: str) -> str:
    return hashlib.sha256(_canonical([seq, ts, kind, data, prev]).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    ts: str
    kind: str
    data: dict[str, Any]
    prev: str
    hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "data": self.data,
            "prev": self.prev,
            "hash": self.hash,
        }


class Journal:
    """Writes entries to an optional file and keeps them in memory for reports."""

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._stream = stream
        self._clock = clock
        self.entries: list[Entry] = []
        self._prev = GENESIS

    @classmethod
    def open(cls, path: Path, *, clock: Callable[[], datetime] | None = None) -> Journal:
        """Open for appending; an existing file is verified and its chain continued."""
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_entries(path) if path.exists() else []
        if existing:
            v = verify(existing)
            if not v.ok:
                raise ValueError(f"{path}: chain broken at seq {v.first_bad_seq}: {v.reason}")
        stream = path.open("a", encoding="utf-8")
        journal = cls(stream, clock=clock or (lambda: datetime.now(UTC)))
        journal.entries = existing
        journal._prev = existing[-1].hash if existing else GENESIS
        return journal

    @property
    def head(self) -> str:
        return self._prev

    def record(self, kind: str, **data: Any) -> Entry:
        seq = len(self.entries)
        ts = self._clock().isoformat()
        data = json.loads(_canonical(data))  # normalise Decimals/datetimes/enums once
        entry = Entry(seq, ts, kind, data, self._prev, entry_hash(seq, ts, kind, data, self._prev))
        self.entries.append(entry)
        self._prev = entry.hash
        if self._stream is not None:
            self._stream.write(_canonical(entry.to_json()) + "\n")
            self._stream.flush()
        return entry

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()

    def of_kind(self, *kinds: str) -> Iterator[Entry]:
        return (e for e in self.entries if e.kind in kinds)


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    entries: int
    first_bad_seq: int | None
    reason: str | None


def read_entries(path: Path) -> list[Entry]:
    entries = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                entries.append(Entry(d["seq"], d["ts"], d["kind"], d["data"], d["prev"], d["hash"]))
    return entries


def verify(entries: list[Entry]) -> Verification:
    prev = GENESIS
    for i, e in enumerate(entries):
        if e.seq != i:
            return Verification(False, len(entries), i, f"seq {e.seq} where {i} expected")
        if e.prev != prev:
            return Verification(False, len(entries), i, "prev hash does not match previous entry")
        if entry_hash(e.seq, e.ts, e.kind, e.data, e.prev) != e.hash:
            return Verification(False, len(entries), i, "entry hash does not match its content")
        prev = e.hash
    return Verification(True, len(entries), None, None)


def verify_file(path: Path) -> Verification:
    return verify(read_entries(path))
