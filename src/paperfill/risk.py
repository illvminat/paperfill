"""Risk envelope: limits, circuit breakers, pause and kill switch.

Every decision is explainable: `check` returns a reason string when it blocks, and the
runner writes both allowed and blocked intents to the journal. Limits are expressed
in USDC notional and in shares so they hold whatever the strategy does. Loss limits
are measured on equity (cash plus positions at mid) against the equity at the start
of the run and of the current UTC day.

The kill switch is a file: its presence stops everything. A file is deliberately
chosen over an in-process flag so an operator (or a dashboard) can stop a run from
outside without talking to the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from paperfill.execution import Portfolio

ZERO = Decimal("0")


class Mode(StrEnum):
    NORMAL = "normal"
    PAUSED = "paused"  # no new orders; open orders stay
    HALTED = "halted"  # breaker tripped or kill switch: everything cancelled, no new orders


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("50")
    max_position_shares: Decimal = Decimal("100")  # per token
    max_market_notional: Decimal = Decimal("100")  # per market (both tokens), cost basis
    max_total_exposure: Decimal = Decimal("200")  # all positions at mid + open order notional
    daily_loss_limit: Decimal = Decimal("20")  # equity drop since UTC day start
    total_loss_limit: Decimal = Decimal("50")  # equity drop since run start


@dataclass(frozen=True, slots=True)
class Intent:
    """What a strategy wants to do; the risk engine judges it before execution."""

    market_id: str
    token_id: str
    side: str
    price: Decimal
    size: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str | None = None


@dataclass(slots=True)
class RiskEngine:
    limits: RiskLimits
    portfolio: Portfolio
    kill_file: Path | None = None
    mode: Mode = Mode.NORMAL
    token_market: dict[str, str] = field(default_factory=dict)
    run_start_equity: Decimal | None = None
    day_start_equity: Decimal | None = None
    day: datetime | None = None
    trip_reason: str | None = None

    def start(self, marks: dict[str, Decimal], now: datetime) -> None:
        eq = self.portfolio.equity(marks)
        self.run_start_equity = eq
        self._roll_day(eq, now)

    def _roll_day(self, equity: Decimal, now: datetime) -> None:
        day = now.astimezone(UTC).date()
        if self.day is None or day != self.day.date():
            self.day = datetime(day.year, day.month, day.day, tzinfo=UTC)
            self.day_start_equity = equity

    def pause(self) -> None:
        if self.mode is Mode.NORMAL:
            self.mode = Mode.PAUSED

    def resume(self) -> None:
        if self.mode is Mode.PAUSED:
            self.mode = Mode.NORMAL

    def kill_requested(self) -> bool:
        return self.kill_file is not None and self.kill_file.exists()

    def evaluate(self, marks: dict[str, Decimal], now: datetime) -> str | None:
        """Update breakers; returns a reason if the engine just halted, else None."""
        if self.mode is Mode.HALTED:
            return None
        if self.kill_requested():
            return self._halt("kill switch file present")
        equity = self.portfolio.equity(marks)
        self._roll_day(equity, now)
        if self.run_start_equity is None or self.day_start_equity is None:
            self.start(marks, now)
            return None
        if self.run_start_equity - equity >= self.limits.total_loss_limit:
            return self._halt(
                f"total loss limit: equity {equity} vs start {self.run_start_equity}, "
                f"limit {self.limits.total_loss_limit}"
            )
        if self.day_start_equity - equity >= self.limits.daily_loss_limit:
            return self._halt(
                f"daily loss limit: equity {equity} vs day start {self.day_start_equity}, "
                f"limit {self.limits.daily_loss_limit}"
            )
        return None

    def _halt(self, reason: str) -> str:
        self.mode, self.trip_reason = Mode.HALTED, reason
        return reason

    def check(self, intent: Intent, marks: dict[str, Decimal], open_notional: Decimal) -> Decision:
        """Judge one intent given current marks and the notional of resting orders."""
        if self.mode is not Mode.NORMAL:
            return Decision(False, f"mode {self.mode.value}: {self.trip_reason or 'paused'}")
        lim = self.limits
        if intent.notional > lim.max_order_notional:
            return Decision(False, f"order notional {intent.notional} > {lim.max_order_notional}")
        pos = self.portfolio.positions.get(intent.token_id)
        held = pos.size if pos else ZERO
        if intent.side == "BUY":
            if held + intent.size > lim.max_position_shares:
                return Decision(
                    False,
                    f"position {held} + {intent.size} > {lim.max_position_shares} shares",
                )
            market_cost = sum(
                (
                    p.cost
                    for t, p in self.portfolio.positions.items()
                    if self.token_market.get(t) == intent.market_id
                ),
                ZERO,
            )
            if market_cost + intent.notional > lim.max_market_notional:
                return Decision(
                    False,
                    f"market notional {market_cost} + {intent.notional} "
                    f"> {lim.max_market_notional}",
                )
            exposure = self.portfolio.exposure(marks) + open_notional + intent.notional
            if exposure > lim.max_total_exposure:
                return Decision(False, f"total exposure {exposure} > {lim.max_total_exposure}")
        elif intent.size > held:
            return Decision(False, f"sell {intent.size} > held {held} (no shorting)")
        return Decision(True)
