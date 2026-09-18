"""Trading fees, computed from the market's own fee schedule.

Source of truth: SOURCES.md -> polymarket/fees.md and polymarket/market-details.md
(section "Trading Fees").

    fee = C * rate * (p * (1 - p)) ** exponent

where C is the number of shares and p the share price. Only the taker pays
(`taker_only`); makers are never charged. Fees are rounded to 5 decimal places; the
smallest non-zero fee is 0.00001 USDC and anything smaller rounds to zero, so tiny
trades near the extremes pay nothing. The rounding *mode* at the fifth decimal is
not stated by the documentation; ROUND_HALF_UP is used here and marked as an
assumption in tests/test_fees.py.

Maker rebates (polymarket/maker-rebates.md) are paid daily from a pool shared by all
makers in proportion to fee-equivalent, so a per-fill rebate can only be estimated;
`maker_rebate_estimate` returns the upper bound `rebate_rate * fee_equivalent`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

FEE_PRECISION = Decimal("0.00001")
"""Fees are rounded to 5 decimal places (polymarket/fees.md, "Fee Precision")."""

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-market fee parameters as served by Gamma (`feeSchedule`)."""

    rate: Decimal
    exponent: Decimal
    taker_only: bool
    rebate_rate: Decimal

    @classmethod
    def free(cls) -> FeeSchedule:
        """A market with `feesEnabled = false` (for example geopolitics)."""
        return cls(rate=_ZERO, exponent=_ONE, taker_only=True, rebate_rate=_ZERO)

    @classmethod
    def from_gamma(
        cls, schedule: dict[str, Any] | None, *, fees_enabled: bool | None
    ) -> FeeSchedule:
        """Build from the Gamma market fields `feeSchedule` and `feesEnabled`.

        Field names are accepted in both spellings Gamma uses (`takerOnly` in raw JSON,
        `taker_only` after the SDK's model_dump).
        """
        if not fees_enabled or not schedule:
            return cls.free()
        rate = Decimal(str(schedule["rate"]))
        exponent = Decimal(str(schedule["exponent"]))
        taker_only = bool(schedule.get("takerOnly", schedule.get("taker_only", True)))
        rebate_rate = Decimal(str(schedule.get("rebateRate", schedule.get("rebate_rate", 0))))
        return cls(rate=rate, exponent=exponent, taker_only=taker_only, rebate_rate=rebate_rate)

    @property
    def fees_enabled(self) -> bool:
        return self.rate != _ZERO

    def fee_equivalent(self, shares: Decimal, price: Decimal) -> Decimal:
        """Unrounded `C * rate * (p * (1 - p)) ** exponent`."""
        if not _ZERO <= price <= _ONE:
            raise ValueError(f"price must be within [0, 1], got {price}")
        if shares < _ZERO:
            raise ValueError(f"shares must be non-negative, got {shares}")
        curve = price * (_ONE - price)
        if self.exponent != _ONE:
            if curve == _ZERO:
                return _ZERO
            curve = curve**self.exponent  # Decimal power; inexact for non-integer exponents
        return shares * self.rate * curve

    def taker_fee(self, shares: Decimal, price: Decimal) -> Decimal:
        """Fee charged to the taker side of a fill, rounded to 5 decimals."""
        return self.fee_equivalent(shares, price).quantize(FEE_PRECISION, rounding=ROUND_HALF_UP)

    def maker_fee(self, shares: Decimal, price: Decimal) -> Decimal:
        """Fee charged to the maker side: zero on every schedule seen so far."""
        if self.taker_only:
            return _ZERO
        return self.taker_fee(shares, price)

    def maker_rebate_estimate(self, shares: Decimal, price: Decimal) -> Decimal:
        """Upper bound of the daily rebate attributable to one maker fill."""
        return (self.taker_fee(shares, price) * self.rebate_rate).quantize(
            FEE_PRECISION, rounding=ROUND_HALF_UP
        )
