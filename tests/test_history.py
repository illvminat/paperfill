"""Trade history: page walk, rate-limit handling, normalisation, storage, determinism."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from polymarket.errors import RateLimitError
from polymarket.models.data.activity import Trade as SdkTrade

from paperfill.history import (
    RateLimited,
    fetch_trades,
    load_trades,
    replay,
    save_trades,
    stream_digest,
)

CID = "0x0499ebe1592dc9d2770cd451b4955fd5093535752b08494779675dd33786a764"


def _raw_trade(ts: int, side: str, price: str, size: str, token: str = "1" * 20) -> dict:
    # Shape of one /v2/trades row (tests/fixtures/data_trades_sample.json), wallet fields
    # included on purpose: the code under test must not let them through.
    return {
        "proxy_wallet": "0x" + "ab" * 20,
        "side": side,
        "token_id": token,
        "condition_id": CID,
        "size": size,
        "price": price,
        "timestamp": ts,
        "transaction_hash": "0x" + "cd" * 32,
        "outcome": "Up",
        "outcome_index": 0,
        "name": "someone",
        "pseudonym": "Some-Name",
    }


class _Page:
    def __init__(self, items, has_more, next_cursor=None):
        self.items, self.has_more, self.next_cursor = tuple(items), has_more, next_cursor


class _Paginator:
    """Mimics the SDK paginator: first_page() fetches the current cursor."""

    def __init__(self, pages, cursor, fail_once_at):
        self._pages, self._cursor, self._fail = pages, cursor, fail_once_at

    def first_page(self):
        if self._cursor in self._fail:
            self._fail.remove(self._cursor)
            raise RateLimitError("slow down", retry_after=3)
        return self._pages[self._cursor]

    def from_cursor(self, cursor):
        return _Paginator(self._pages, cursor, self._fail)


class _Source:
    def __init__(self, pages, fail_once_at=()):
        self.pages, self.fail, self.calls = pages, set(fail_once_at), []

    def list_trades(self, **params):
        self.calls.append(params)
        return _Paginator(self.pages, None, self.fail)


def _pages():
    # API order: newest first, across two pages.
    p1 = [_raw_trade(1000, "SELL", "0.60", "10"), _raw_trade(999, "BUY", "0.59", "5")]
    p2 = [_raw_trade(999, "BUY", "0.58", "7"), _raw_trade(900, "BUY", "0.50", "1")]
    return {
        None: _Page((SdkTrade.model_validate(r) for r in p1), True, "c2"),
        "c2": _Page((SdkTrade.model_validate(r) for r in p2), False),
    }


def test_walks_every_page_and_counts_all_rows():
    src = _Source(_pages())
    trades = fetch_trades(src, CID, sleep=lambda s: None)
    assert len(trades) == 4  # sum of both pages
    assert src.calls == [{"condition_id": CID, "page_size": 1000, "taker_only": True}]


def test_oldest_first_with_stable_ties_and_dense_seq():
    trades = fetch_trades(_Source(_pages()), CID, sleep=lambda s: None)
    assert [t.ts for t in trades] == sorted(t.ts for t in trades)
    assert [t.seq for t in trades] == [0, 1, 2, 3]
    # two prints at ts=999: reversed API order puts page-2 row (0.58) before page-1 row (0.59)
    assert [str(t.price) for t in trades] == ["0.50", "0.58", "0.59", "0.60"]
    assert trades[0].ts == datetime(1970, 1, 1, 0, 15, tzinfo=UTC)


def test_rate_limit_waits_server_delay_and_loses_no_page():
    waits, events = [], []
    src = _Source(_pages(), fail_once_at=["c2"])
    trades = fetch_trades(src, CID, sleep=waits.append, on_event=events.append)
    assert len(trades) == 4
    assert waits == [3.0]
    assert events == [RateLimited(cursor="c2", retry_after=3.0, attempt=1)]


def test_rate_limit_longer_than_allowed_is_raised():
    src = _Source(_pages(), fail_once_at=[None])
    with pytest.raises(RateLimitError):
        fetch_trades(src, CID, sleep=lambda s: None, max_retry_after=2)


def test_no_wallet_or_hash_survives_normalisation():
    trades = fetch_trades(_Source(_pages()), CID, sleep=lambda s: None)
    blob = " ".join(str(t.to_json()) for t in trades)
    assert "ab" * 20 not in blob and "cd" * 32 not in blob and "someone" not in blob


def test_save_and_load_roundtrip(tmp_path):
    trades = fetch_trades(_Source(_pages()), CID, sleep=lambda s: None)
    path = tmp_path / f"{CID}.jsonl"
    save_trades(path, CID, trades, fetched_at=datetime(2026, 9, 19, tzinfo=UTC))
    header, loaded = load_trades(path)
    assert header["count"] == 4 and header["condition_id"] == CID
    assert loaded == trades
    assert "proxy_wallet" not in path.read_text()


def test_load_rejects_truncated_file(tmp_path):
    trades = fetch_trades(_Source(_pages()), CID, sleep=lambda s: None)
    path = tmp_path / "h.jsonl"
    save_trades(path, CID, trades, fetched_at=datetime(2026, 9, 19, tzinfo=UTC))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")
    with pytest.raises(ValueError, match="header says 4"):
        load_trades(path)


def test_replay_is_deterministic_regardless_of_input_order():
    trades = fetch_trades(_Source(_pages()), CID, sleep=lambda s: None)
    shuffled = [trades[2], trades[0], trades[3], trades[1]]
    assert list(replay(shuffled)) == trades
    assert stream_digest(shuffled) == stream_digest(trades)
    changed = [*trades[:-1], replace(trades[-1], size=Decimal("11"))]
    assert stream_digest(changed) != stream_digest(trades)


@pytest.mark.live
def test_live_full_history_of_a_closed_market():
    from polymarket import PublicClient

    client = PublicClient()
    try:
        # BTC Up or Down, April 2 2026 9:50-9:55 PM ET (docs/measurements/2026-09-19-api-probe.md)
        trades = fetch_trades(
            client, "0x29789033e9636c68c85f55bc4731d6ffbe8f41d37caf0df655a383b626e29c23"
        )
    finally:
        client.close()
    assert len(trades) == 5347
    assert trades == list(replay(trades))
