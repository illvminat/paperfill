"""Trade history of one market, fetched from the public Data API and replayed as a stream.

Source of truth: SOURCES.md -> polymarket/data-api-openapi.json (`/v2/trades`: keyset
cursor, `limit` up to 1000, a three-year window when filtered by condition, public,
and `429` with `Retry-After` on quota exhaustion).

The SDK retries a 429 at most twice and only when the requested delay is short
(polymarket/_internal/retry.py, read 2026-09-19). A full history walk needs more
patience than that, so pages are fetched here one cursor at a time: on a rate limit
the same cursor is retried after the server's delay, every wait is reported, and no
page is skipped.

Only market data is kept. Wallet addresses, transaction hashes and profile fields
present in the API response are dropped before anything is written to disk
(docs/zadanie.md, section 3: no personal data).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from polymarket.errors import RateLimitError

Side = Literal["BUY", "SELL"]

DEFAULT_PAGE_SIZE = 1000
"""`limit` maximum for `/v2/trades` (data-api-openapi.json)."""


@dataclass(frozen=True, slots=True, order=True)
class TradePrint:
    """One taker fill as printed by the market. Ordered by (timestamp, seq)."""

    ts: datetime
    seq: int
    side: Side
    price: Decimal
    size: Decimal
    token_id: str
    outcome: str | None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["price"], d["size"] = str(self.price), str(self.size)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TradePrint:
        return cls(
            ts=datetime.fromisoformat(d["ts"]),
            seq=int(d["seq"]),
            side=d["side"],
            price=Decimal(d["price"]),
            size=Decimal(d["size"]),
            token_id=d["token_id"],
            outcome=d.get("outcome"),
        )


@dataclass(frozen=True, slots=True)
class RateLimited:
    """Reported through `on_event` whenever a page fetch had to wait."""

    cursor: str | None
    retry_after: float
    attempt: int


class TradeSource(Protocol):
    def list_trades(self, **params: Any) -> Any: ...


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fetch_trades(
    source: TradeSource,
    condition_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    sleep: Callable[[float], None] = time.sleep,
    max_retry_after: float = 120.0,
    max_pages: int = 10_000,
    on_event: Callable[[RateLimited], None] | None = None,
) -> list[TradePrint]:
    """Walk every page of taker trades for `condition_id`, oldest first.

    Raises `RateLimitError` only if the server asks to wait longer than
    `max_retry_after` seconds; shorter waits are honoured and reported.
    """
    paginator = source.list_trades(condition_id=condition_id, page_size=page_size, taker_only=True)
    raw: list[Any] = []
    cursor: str | None = None
    seen: set[str | None] = {None}
    attempt = 0
    while True:
        try:
            page = paginator.first_page()
        except RateLimitError as error:
            delay = float(error.retry_after) if error.retry_after is not None else 1.0
            if delay > max_retry_after:
                raise
            attempt += 1
            if on_event:
                on_event(RateLimited(cursor=cursor, retry_after=delay, attempt=attempt))
            sleep(max(0.0, delay))
            continue
        attempt = 0
        raw.extend(page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
        if cursor in seen:
            raise RuntimeError(f"cursor {cursor!r} repeated: refusing to loop")
        seen.add(cursor)
        if len(seen) > max_pages:
            raise RuntimeError(f"more than {max_pages} pages for one market: refusing to continue")
        paginator = paginator.from_cursor(cursor)
    return _normalise(raw)


def _normalise(raw: Iterable[Any]) -> list[TradePrint]:
    """Newest-first API order becomes oldest-first, stable, with a dense `seq`.

    Timestamps have one-second resolution, so the order of prints within a second is
    the API's own order (reversed). Determinism therefore holds for a given API
    response, not across responses that order intra-second prints differently.
    """
    rows = list(raw)
    rows.reverse()
    rows.sort(key=lambda t: _as_utc(t.timestamp))  # stable: ties keep API order reversed
    return [
        TradePrint(
            ts=_as_utc(t.timestamp),
            seq=i,
            side="BUY" if str(getattr(t.side, "value", t.side)).upper() == "BUY" else "SELL",
            price=Decimal(str(t.price)),
            size=Decimal(str(t.size)),
            token_id=str(t.asset_id),
            outcome=t.outcome,
        )
        for i, t in enumerate(rows)
    ]


def history_path(root: Path, condition_id: str) -> Path:
    return root / f"{condition_id}.jsonl"


def save_trades(
    path: Path, condition_id: str, trades: list[TradePrint], *, fetched_at: datetime
) -> None:
    """Write a header line followed by one JSON object per trade (append-only file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        header = {
            "kind": "header",
            "condition_id": condition_id,
            "fetched_at": fetched_at.isoformat(),
            "count": len(trades),
        }
        f.write(json.dumps(header) + "\n")
        for t in trades:
            f.write(json.dumps(t.to_json()) + "\n")
    tmp.replace(path)


def load_trades(path: Path) -> tuple[dict[str, Any], list[TradePrint]]:
    with path.open(encoding="utf-8") as f:
        header = json.loads(f.readline())
        if header.get("kind") != "header":
            raise ValueError(f"{path}: first line is not a history header")
        trades = [TradePrint.from_json(json.loads(line)) for line in f if line.strip()]
    if header["count"] != len(trades):
        raise ValueError(f"{path}: header says {header['count']} trades, file has {len(trades)}")
    return header, trades


def replay(trades: Iterable[TradePrint]) -> Iterator[TradePrint]:
    """Yield prints in deterministic order regardless of input order."""
    yield from sorted(trades)


def stream_digest(trades: Iterable[TradePrint]) -> str:
    """SHA-256 over the canonical serialisation; equal digests mean equal streams."""
    import hashlib

    h = hashlib.sha256()
    for t in replay(trades):
        h.update(json.dumps(t.to_json(), sort_keys=True).encode())
    return h.hexdigest()
