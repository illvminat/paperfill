"""Market discovery: open "Up or Down" crypto markets and their trading parameters.

Transport and parsing come from the official SDK (`polymarket-client`, SOURCES.md ->
polymarket/python-sdk.md). Everything paperfill needs from a market is copied into
`MarketInfo` so the rest of the code never touches SDK models.

Asset and window are read from the market slug, e.g. `btc-updown-5m-1789768200`.
This is an observed convention (tests/fixtures/gamma_market_btc_updown.json), not a
documented contract; markets whose slug does not match are reported with
`asset=None, window=None` rather than dropped silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from polymarket.models.gamma.market import Market as SdkMarket

from paperfill.fees import FeeSchedule

_SLUG = re.compile(r"^(?P<asset>[a-z0-9]+)-updown-(?P<window>\d+[mh])-(?P<ts>\d+)$")


@dataclass(frozen=True, slots=True)
class MarketInfo:
    condition_id: str
    question: str
    slug: str
    asset: str | None
    window: str | None
    yes_token: str
    no_token: str
    yes_label: str
    no_label: str
    start: datetime | None
    end: datetime | None
    active: bool
    closed: bool
    accepting_orders: bool
    neg_risk: bool
    min_order_size: Decimal
    tick_size: Decimal
    fees: FeeSchedule
    rewards_min_size: Decimal | None
    rewards_max_spread: Decimal | None
    resolved: bool = False
    yes_payout: Decimal | None = None
    no_payout: Decimal | None = None

    @property
    def tradeable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders

    @property
    def payouts(self) -> dict[str, Decimal] | None:
        """Payout per share for each token once the market is resolved, else None.

        Observed 2026-09-19 on a closed 5-minute market: `resolution.uma_resolution_status`
        is "resolved" and the outcome prices are exactly "0" and "1"
        (docs/measurements/2026-09-19-api-probe.md).
        """
        if not self.resolved or self.yes_payout is None or self.no_payout is None:
            return None
        return {self.yes_token: self.yes_payout, self.no_token: self.no_payout}


def parse_slug(slug: str) -> tuple[str | None, str | None]:
    m = _SLUG.match(slug)
    if not m:
        return None, None
    return m["asset"].upper(), m["window"]


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def from_sdk(market: SdkMarket) -> MarketInfo:
    d = market.model_dump(mode="json")
    state, outcomes, trading, rewards = d["state"], d["outcomes"], d["trading"], d["rewards"]
    asset, window = parse_slug(d["slug"] or "")
    resolved = (
        bool(state["closed"])
        and (d.get("resolution") or {}).get("uma_resolution_status") == "resolved"
    )
    return MarketInfo(
        condition_id=d["condition_id"],
        question=d["question"],
        slug=d["slug"],
        asset=asset,
        window=window,
        yes_token=outcomes["yes"]["token_id"],
        no_token=outcomes["no"]["token_id"],
        yes_label=outcomes["yes"]["label"],
        no_label=outcomes["no"]["label"],
        start=_dt(state["start_date"]),
        end=_dt(state["end_date"]),
        active=bool(state["active"]),
        closed=bool(state["closed"]),
        accepting_orders=bool(state["accepting_orders"]),
        neg_risk=bool(state["neg_risk"]),
        min_order_size=Decimal(str(trading["minimum_order_size"])),
        tick_size=Decimal(str(trading["minimum_tick_size"])),
        fees=FeeSchedule.from_gamma(trading["fee_schedule"], fees_enabled=trading["fees_enabled"]),
        rewards_min_size=_dec(rewards.get("rewards_min_size")),
        rewards_max_spread=_dec(rewards.get("rewards_max_spread")),
        resolved=resolved,
        yes_payout=_dec(outcomes["yes"].get("price")) if resolved else None,
        no_payout=_dec(outcomes["no"].get("price")) if resolved else None,
    )


def from_gamma_json(raw: dict[str, Any]) -> MarketInfo:
    """Parse one raw Gamma `/markets` object (the shape stored in tests/fixtures)."""
    return from_sdk(SdkMarket.model_validate(raw))


class MarketSource(Protocol):
    """The slice of the SDK client that discovery needs; tests substitute a fake."""

    def list_markets(self, **params: Any) -> Any: ...


def discover(
    source: MarketSource,
    *,
    asset: str | None = None,
    window: str | None = None,
    now: datetime | None = None,
    page_size: int = 100,
    limit: int = 20,
) -> Iterator[MarketInfo]:
    """Yield open Up/Down markets ending soonest first, filtered by asset and window.

    Query parameters follow the SDK signature observed in
    docs/measurements/2026-09-19-api-probe.md: `closed=False`, `order="endDate"`,
    `ascending=True`, `end_date_min=<now>`.
    """
    now = now or datetime.now(UTC)
    pages = source.list_markets(
        closed=False,
        order="endDate",
        ascending=True,
        end_date_min=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        page_size=page_size,
    )
    yielded = 0
    for page in pages:
        items: Iterable[SdkMarket] = page.items
        for sdk_market in items:
            info = from_sdk(sdk_market)
            if info.window is None:
                continue
            if asset and info.asset != asset.upper():
                continue
            if window and info.window != window:
                continue
            yield info
            yielded += 1
            if yielded >= limit:
                return
