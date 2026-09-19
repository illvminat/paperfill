"""Venue interface: what paperfill needs from a market-data source, and the one venue it ships.

A client who holds a licence for another venue's data implements this Protocol on their
side; paperfill never fetches, stores or redistributes data it is not allowed to
(docs/decisions/0004-kalshi.md). The Polymarket implementation wraps the modules that
already exist so nothing is duplicated.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from paperfill.book import OrderBook
from paperfill.history import TradePrint
from paperfill.markets import MarketInfo


@runtime_checkable
class Venue(Protocol):
    """Read-only market data for binary markets with a yes/no token pair."""

    name: str

    def discover(self, *, asset: str | None, window: str | None, limit: int) -> list[MarketInfo]:
        """Open markets, soonest to end first."""

    def market(self, condition_id: str) -> MarketInfo | None:
        """One market by its venue id, resolved payouts included when settled."""

    def order_book(self, token_id: str) -> OrderBook:
        """Current book for one outcome token."""

    def trades(self, condition_id: str) -> list[TradePrint]:
        """Full taker trade history, oldest first, without personal fields."""

    def record(
        self, market: MarketInfo, path: Path, *, seconds: float | None, with_prices: bool
    ) -> int:
        """Record the live stream of a market to `path`; returns the event count."""


class PolymarketVenue:
    """The shipped venue: public Polymarket APIs through the official SDK."""

    name = "polymarket"

    def __init__(self, client_factory: Any | None = None) -> None:
        self._factory = client_factory

    def _client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from polymarket import PublicClient

        return PublicClient()

    def discover(self, *, asset: str | None, window: str | None, limit: int) -> list[MarketInfo]:
        from paperfill.markets import discover

        client = self._client()
        try:
            return list(discover(client, asset=asset, window=window, limit=limit))
        finally:
            _close(client)

    def market(self, condition_id: str) -> MarketInfo | None:
        from paperfill.markets import from_sdk

        client = self._client()
        try:
            for closed in (False, True):
                page = client.list_markets(
                    condition_ids=[condition_id], closed=closed, page_size=1
                ).first_page()
                if page.items:
                    return from_sdk(page.items[0])
            return None
        finally:
            _close(client)

    def order_book(self, token_id: str) -> OrderBook:
        client = self._client()
        try:
            return OrderBook.from_snapshot(
                client.get_order_book(token_id=token_id).model_dump(mode="json")
            )
        finally:
            _close(client)

    def trades(self, condition_id: str) -> list[TradePrint]:
        from paperfill.history import fetch_trades

        client = self._client()
        try:
            return fetch_trades(client, condition_id)
        finally:
            _close(client)

    def record(
        self, market: MarketInfo, path: Path, *, seconds: float | None, with_prices: bool
    ) -> int:
        import asyncio

        from paperfill.recorder import JsonlSink, record_market

        def async_client() -> Any:
            from polymarket import AsyncPublicClient

            return AsyncPublicClient()

        sink = JsonlSink(path)
        try:
            return asyncio.run(
                record_market(
                    async_client,
                    [market.yes_token, market.no_token],
                    sink,
                    seconds=seconds,
                    price_symbols=market.price_symbols if with_prices else None,
                )
            )
        finally:
            sink.close()


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close:
        close()


def events_of(path: Path) -> Iterator[Any]:
    """Recording events regardless of venue: the file format is venue-neutral."""
    from paperfill.runner import events_from_recording

    return events_from_recording(path)


def first_event_time(path: Path) -> datetime | None:
    for e in events_of(path):
        return e.ts
    return None
