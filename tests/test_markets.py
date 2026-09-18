"""Market discovery: parsing a recorded Gamma response and filtering."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from polymarket.models.gamma.market import Market as SdkMarket

from paperfill.markets import discover, from_gamma_json, parse_slug


def test_fixture_parses_into_market_info(gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    assert m.condition_id == gamma_market_raw["conditionId"]
    assert m.question.startswith("Bitcoin Up or Down")
    assert (m.asset, m.window) == ("BTC", "5m")
    assert m.yes_label == "Up" and m.no_label == "Down"
    assert m.yes_token != m.no_token and m.yes_token.isdigit()
    assert m.end == datetime(2026, 9, 18, 21, 55, tzinfo=UTC)
    assert m.min_order_size == Decimal("5") and m.tick_size == Decimal("0.01")
    assert m.fees.rate == Decimal("0.07") and m.fees.rebate_rate == Decimal("0.2")
    assert m.fees.taker_only and m.fees.fees_enabled
    assert m.rewards_min_size == Decimal("50") and m.rewards_max_spread == Decimal("4.5")
    assert m.tradeable and not m.neg_risk


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("btc-updown-5m-1789768200", ("BTC", "5m")),
        ("eth-updown-15m-1789768200", ("ETH", "15m")),
        ("will-lebron-james-retire-before-next-nba-season", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_slug(slug, expected):
    assert parse_slug(slug) == expected


class _Page:
    def __init__(self, items):
        self.items = items


class _FakeSource:
    def __init__(self, raws):
        self.raws = raws
        self.calls = []

    def list_markets(self, **params):
        self.calls.append(params)
        return [_Page(tuple(SdkMarket.model_validate(r) for r in self.raws))]


def _variant(raw, slug, question):
    v = dict(raw)
    v["slug"], v["question"] = slug, question
    return v


def test_discover_filters_by_asset_and_window_and_skips_non_updown(gamma_market_raw):
    src = _FakeSource(
        [
            gamma_market_raw,
            _variant(gamma_market_raw, "eth-updown-5m-1", "Ethereum Up or Down"),
            _variant(gamma_market_raw, "btc-updown-15m-1", "Bitcoin Up or Down 15"),
            _variant(gamma_market_raw, "will-x-happen", "Will X happen?"),
        ]
    )
    now = datetime(2026, 9, 18, 21, 50, tzinfo=UTC)
    got = list(discover(src, asset="btc", window="5m", now=now))
    assert [m.slug for m in got] == ["btc-updown-5m-1789768200"]
    assert src.calls == [
        {
            "closed": False,
            "order": "endDate",
            "ascending": True,
            "end_date_min": "2026-09-18T21:50:00Z",
            "page_size": 100,
        }
    ]
    assert len(list(discover(src, now=now))) == 3  # every Up/Down market, any asset
    assert len(list(discover(src, now=now, limit=2))) == 2


@pytest.mark.live
def test_live_discover_returns_open_updown_markets():
    from polymarket import PublicClient

    client = PublicClient()
    try:
        got = list(discover(client, limit=3))
    finally:
        client.close()
    assert got and all(m.window is not None and m.tradeable for m in got)
