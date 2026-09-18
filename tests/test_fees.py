"""Fee formula against the examples published in the documentation.

Reference values are the "Fee Tables (100 Shares)" from polymarket/fees.md
(SOURCES.md), copied by hand, not produced by this implementation. The tables show
cents, so the comparison is made at two decimals; the 5-decimal precision rule is
tested separately.
"""

from decimal import Decimal

import pytest

from paperfill.fees import FEE_PRECISION, FeeSchedule

# (price, taker fee in USDC for 100 shares) per category, verbatim from the docs.
CRYPTO = [
    ("0.01", "0.07"), ("0.05", "0.33"), ("0.10", "0.63"), ("0.15", "0.89"),
    ("0.20", "1.12"), ("0.25", "1.31"), ("0.30", "1.47"), ("0.35", "1.59"),
    ("0.40", "1.68"), ("0.45", "1.73"), ("0.50", "1.75"), ("0.55", "1.73"),
    ("0.60", "1.68"), ("0.65", "1.59"), ("0.70", "1.47"), ("0.75", "1.31"),
    ("0.80", "1.12"), ("0.85", "0.89"), ("0.90", "0.63"), ("0.95", "0.33"),
    ("0.99", "0.07"),
]  # fmt: skip
SPORTS = [
    ("0.01", "0.05"), ("0.05", "0.24"), ("0.10", "0.45"), ("0.15", "0.64"),
    ("0.20", "0.80"), ("0.25", "0.94"), ("0.30", "1.05"), ("0.35", "1.14"),
    ("0.40", "1.20"), ("0.45", "1.24"), ("0.50", "1.25"), ("0.55", "1.24"),
    ("0.60", "1.20"), ("0.65", "1.14"), ("0.70", "1.05"), ("0.75", "0.94"),
    ("0.80", "0.80"), ("0.85", "0.64"), ("0.90", "0.45"), ("0.95", "0.24"),
    ("0.99", "0.05"),
]  # fmt: skip
FINANCE = [
    ("0.01", "0.04"), ("0.05", "0.19"), ("0.10", "0.36"), ("0.15", "0.51"),
    ("0.20", "0.64"), ("0.25", "0.75"), ("0.30", "0.84"), ("0.35", "0.91"),
    ("0.40", "0.96"), ("0.45", "0.99"), ("0.50", "1.00"), ("0.55", "0.99"),
    ("0.60", "0.96"), ("0.65", "0.91"), ("0.70", "0.84"), ("0.75", "0.75"),
    ("0.80", "0.64"), ("0.85", "0.51"), ("0.90", "0.36"), ("0.95", "0.19"),
]  # fmt: skip
ECONOMICS = [
    ("0.01", "0.05"), ("0.05", "0.24"), ("0.10", "0.45"), ("0.15", "0.64"),
    ("0.20", "0.80"), ("0.25", "0.94"), ("0.30", "1.05"), ("0.35", "1.14"),
    ("0.40", "1.20"), ("0.45", "1.24"), ("0.50", "1.25"), ("0.55", "1.24"),
    ("0.60", "1.20"), ("0.65", "1.14"), ("0.70", "1.05"), ("0.75", "0.94"),
    ("0.80", "0.80"), ("0.85", "0.64"), ("0.90", "0.45"), ("0.95", "0.24"),
    ("0.99", "0.05"),
]  # fmt: skip


def schedule(rate: str, rebate: str) -> FeeSchedule:
    return FeeSchedule.from_gamma(
        {"rate": rate, "exponent": 1, "takerOnly": True, "rebateRate": rebate}, fees_enabled=True
    )


CASES = (
    [("crypto", "0.07", p, f) for p, f in CRYPTO]
    + [("sports", "0.05", p, f) for p, f in SPORTS]
    + [("finance", "0.04", p, f) for p, f in FINANCE]
    + [("economics", "0.05", p, f) for p, f in ECONOMICS]
)


@pytest.mark.parametrize(("category", "rate", "price", "expected"), CASES)
def test_taker_fee_matches_documentation_tables(category, rate, price, expected):
    fee = schedule(rate, "0.25").taker_fee(Decimal("100"), Decimal(price))
    assert fee.quantize(Decimal("0.01")) == Decimal(expected), category


def test_fee_is_symmetric_around_one_half():
    s = schedule("0.07", "0.2")
    assert s.taker_fee(Decimal("100"), Decimal("0.3")) == s.taker_fee(
        Decimal("100"), Decimal("0.7")
    )


def test_fee_rounds_to_five_decimals_and_tiny_fees_vanish():
    s = schedule("0.07", "0.2")
    # 1 share at 0.01: 0.07 * 0.01 * 0.99 = 0.000693 -> 0.00069 (5 decimals)
    assert s.taker_fee(Decimal("1"), Decimal("0.01")) == Decimal("0.00069")
    # 0.001 share at 0.01: 0.000000693 -> rounds to zero, as the docs say may happen
    assert s.taker_fee(Decimal("0.001"), Decimal("0.01")) == Decimal("0")
    assert (
        s.taker_fee(Decimal("100"), Decimal("0.5")).as_tuple().exponent
        >= FEE_PRECISION.as_tuple().exponent
    )


def test_maker_pays_nothing_on_taker_only_schedule():
    s = schedule("0.07", "0.2")
    assert s.maker_fee(Decimal("100"), Decimal("0.5")) == Decimal("0")


def test_maker_rebate_estimate_is_rate_times_fee():
    s = schedule("0.07", "0.2")
    assert s.maker_rebate_estimate(Decimal("100"), Decimal("0.5")) == Decimal("0.35")


def test_disabled_fees_are_zero_everywhere():
    s = FeeSchedule.from_gamma({"rate": "0.07", "exponent": 1}, fees_enabled=False)
    assert not s.fees_enabled
    assert s.taker_fee(Decimal("100"), Decimal("0.5")) == Decimal("0")
    assert FeeSchedule.from_gamma(None, fees_enabled=True) == FeeSchedule.free()


def test_snake_case_keys_from_sdk_dump_are_accepted():
    s = FeeSchedule.from_gamma(
        {"rate": "0.04", "exponent": 1, "taker_only": True, "rebate_rate": "0.25"},
        fees_enabled=True,
    )
    assert s.rate == Decimal("0.04") and s.rebate_rate == Decimal("0.25")


@pytest.mark.parametrize("price", ["-0.01", "1.01"])
def test_price_outside_unit_interval_is_rejected(price):
    with pytest.raises(ValueError):
        schedule("0.07", "0.2").taker_fee(Decimal("1"), Decimal(price))


def test_non_unit_exponent_uses_decimal_arithmetic():
    s = FeeSchedule.from_gamma(
        {"rate": "0.1", "exponent": 2, "takerOnly": True, "rebateRate": "0"}, fees_enabled=True
    )
    # 100 * 0.1 * (0.5 * 0.5) ** 2 = 10 * 0.0625 = 0.625
    assert s.taker_fee(Decimal("100"), Decimal("0.5")) == Decimal("0.62500")
    assert s.taker_fee(Decimal("100"), Decimal("1")) == Decimal("0")
