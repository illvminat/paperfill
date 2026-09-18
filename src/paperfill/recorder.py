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
    max_reconnect_delay: float = 60.0,
    max_events: int | None = None,
    on_connection_closed: Callable[[Any], Any] | None = None,
) -> int:
    """Consume events until `stop` is set (or `max_events` written); return the count.

    Reconnection waits `reconnect_delay`, doubling after each consecutive failure up to
    `max_reconnect_delay`, and resets after a connection that delivered an event. Every
    handle returned by `subscribe` is passed to `on_connection_closed` when it ends.
    """
    written = 0
    failures = 0
    sink.write({"kind": "start", "recv_ts": clock().isoformat()})
    while not stop.is_set():
        delivered = False
        handle = None
        try:
            handle = subscribe()
            if asyncio.iscoroutine(handle):
                handle = await handle
            async with handle as stream:
                async for event in stream:
                    sink.write(event_record(event, clock()))
                    written += 1
                    delivered = True
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
        finally:
            if on_connection_closed is not None and handle is not None:
                result = on_connection_closed(handle)
                if asyncio.iscoroutine(result):
                    await result
        if not stop.is_set():
            failures = 0 if delivered else failures + 1
            delay = min(max_reconnect_delay, reconnect_delay * (2 ** max(0, failures - 1)))
            sink.write({"kind": "reconnect", "recv_ts": clock().isoformat(), "delay": delay})
            await sleep(delay)
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
    """Record `token_ids`; a fresh AsyncPublicClient per connection, closed when it ends."""
    from polymarket.streams import MarketSpec

    stop = asyncio.Event()
    handles: dict[int, Any] = {}

    def subscribe() -> Any:
        client = client_factory()
        handle = client.subscribe(MarketSpec(token_ids=token_ids))
        handles[id(handle)] = client
        return handle

    async def closed(handle: Any) -> None:
        client = handles.pop(id(handle), None)
        close = getattr(client, "close", None) or getattr(client, "aclose", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def timer() -> None:
        if seconds is not None:
            await asyncio.sleep(seconds)
            stop.set()

    timer_task = asyncio.create_task(timer())
    recorder = asyncio.create_task(
        run_recorder(
            subscribe,
            sink,
            stop=stop,
            reconnect_delay=reconnect_delay,
            on_connection_closed=closed,
        )
    )
    try:
        done, _ = await asyncio.wait({timer_task, recorder}, return_when=asyncio.FIRST_COMPLETED)
        if recorder in done:
            return recorder.result()
        # the timer fired: the recorder notices `stop` at its next event; if the stream is
        # silent, cancel it and record what was written so far
        try:
            return await asyncio.wait_for(recorder, timeout=5)
        except TimeoutError:
            recorder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recorder
            written = sum(1 for r in getattr(sink, "records", []) if "payload" in r)
            sink.write(
                {
                    "kind": "stop",
                    "recv_ts": datetime.now(UTC).isoformat(),
                    "events": written,
                    "reason": "cancelled while the stream was silent",
                }
            )
            return written
    finally:
        timer_task.cancel()
        for client in list(handles.values()):
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
