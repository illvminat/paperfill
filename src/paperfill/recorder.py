"""Record the live market stream of one or more tokens to an append-only file.

Source of truth: SOURCES.md -> polymarket/realtime-data.md ("Market Stream") and
polymarket/python-sdk.md ("Realtime Subscriptions"). The SDK owns the socket, the
`PING` heartbeat and message parsing; this module owns persistence and gaps.

Every received event is written as one JSON line with the local receive time. When
the stream ends or fails for any reason other than a requested stop, a `gap` record
is written first and the subscription is re-established after a delay. A gap is data:
downstream code must treat the book as unknown until the next `book` snapshot.

Transaction hashes carried by `last_trade_price` events are dropped before writing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

SubscribeFactory = Callable[[], "AbstractAsyncContextManager[AsyncIterator[Any]] | Any"]
"""Returns an async context manager yielding an async iterator of SDK market events."""


class Sink(Protocol):
    def write(self, record: dict[str, Any]) -> None: ...


class JsonlSink:
    """Append-only JSON lines file; one flush per record so a crash loses at most one."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._f.write(json.dumps(record, sort_keys=True) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class ListSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


_DROP = {"transaction_hash"}


def event_record(event: Any, recv_ts: datetime) -> dict[str, Any]:
    """Serialise one SDK market event: `{kind, recv_ts, payload}` without hashes."""
    payload = event.payload.model_dump(mode="json")
    for key in _DROP:
        payload.pop(key, None)
    return {"kind": event.type, "recv_ts": recv_ts.isoformat(), "payload": payload}


async def run_recorder(
    subscribe: SubscribeFactory,
    sink: Sink,
    *,
    stop: asyncio.Event,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Any] = asyncio.sleep,
    reconnect_delay: float = 2.0,
    max_events: int | None = None,
) -> int:
    """Consume events until `stop` is set (or `max_events` written); return the count."""
    written = 0
    sink.write({"kind": "start", "recv_ts": clock().isoformat()})
    while not stop.is_set():
        try:
            handle = subscribe()
            if asyncio.iscoroutine(handle):
                handle = await handle
            async with handle as stream:
                async for event in stream:
                    sink.write(event_record(event, clock()))
                    written += 1
                    if max_events is not None and written >= max_events:
                        stop.set()
                    if stop.is_set():
                        break
            if not stop.is_set():
                sink.write(
                    {"kind": "gap", "recv_ts": clock().isoformat(), "reason": "stream ended"}
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            sink.write(
                {
                    "kind": "gap",
                    "recv_ts": clock().isoformat(),
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
        if not stop.is_set():
            await sleep(reconnect_delay)
    sink.write({"kind": "stop", "recv_ts": clock().isoformat(), "events": written})
    return written


def record_path(root: Path, condition_id: str, started: datetime) -> Path:
    return root / f"{condition_id}-{started.strftime('%Y%m%dT%H%M%SZ')}.jsonl"


async def record_market(
    client_factory: Callable[[], Any],
    token_ids: list[str],
    sink: Sink,
    *,
    seconds: float | None,
    reconnect_delay: float = 2.0,
) -> int:
    """Record `token_ids` with a fresh AsyncPublicClient per (re)connection."""
    from polymarket.streams import MarketSpec

    stop = asyncio.Event()
    clients: list[Any] = []

    def subscribe():
        client = client_factory()
        clients.append(client)
        return client.subscribe(MarketSpec(token_ids=token_ids))

    async def timer() -> None:
        if seconds is not None:
            await asyncio.sleep(seconds)
            stop.set()

    timer_task = asyncio.create_task(timer())
    recorder = asyncio.create_task(
        run_recorder(subscribe, sink, stop=stop, reconnect_delay=reconnect_delay)
    )
    try:
        done, _ = await asyncio.wait({timer_task, recorder}, return_when=asyncio.FIRST_COMPLETED)
        if recorder not in done:
            # the timer fired: let the recorder notice `stop` at the next event, or cancel
            try:
                return await asyncio.wait_for(recorder, timeout=5)
            except TimeoutError:
                recorder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recorder
                sink.write({"kind": "stop", "recv_ts": datetime.now(UTC).isoformat(), "events": -1})
                return -1
        return recorder.result()
    finally:
        timer_task.cancel()
        for c in clients:
            close = getattr(c, "close", None) or getattr(c, "aclose", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
