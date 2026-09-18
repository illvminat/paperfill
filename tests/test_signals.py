"""Fair value model: limits and monotonicity worked out by hand."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.signals import FairValue

START = datetime(2026, 9, 18, 21, 50, tzinfo=UTC)
END = START + timedelta(minutes=5)


def fv() -> FairValue:
    f = FairValue(START, END)
    f.on_reference(START, D("80000"))
    # spot prints 1 s apart with ±0.05% moves -> sigma ~ 5e-4 per second
    p = 80000.0
    for i in range(1, 61):
        p *= 1.0005 if i % 2 else 1 / 1.0005
        f.on_spot(START + timedelta(seconds=i), D(str(round(p, 2))))
    return f


def test_at_start_price_fair_value_is_one_half():
    f = fv()
    f.on_reference(START + timedelta(seconds=60), D("80000"))
    assert abs(f.p_up(START + timedelta(seconds=60)) - 0.5) < 1e-9
    assert f.start_source == "twap-at-start"


def test_higher_reference_means_higher_p_up_and_it_sharpens_near_the_end():
    f = fv()
    f.on_reference(START + timedelta(seconds=60), D("80040"))  # +0.05%
    early = f.p_up(START + timedelta(seconds=60))
    late = f.p_up(END - timedelta(seconds=2))
    assert 0.5 < early < late < 1.0
    f.on_reference(START + timedelta(seconds=60), D("79960"))
    assert f.p_up(START + timedelta(seconds=60)) < 0.5


def test_no_fair_value_before_both_feeds_and_mid_window_start_is_flagged():
    f = FairValue(START, END)
    assert f.p_up(START) is None
    f.on_reference(START + timedelta(seconds=90), D("80000"))
    assert f.start_source == "first-seen"
    assert f.p_up(START + timedelta(seconds=90)) == 0.5  # sigma floored, price at start


def test_sigma_is_floored_before_data():
    f = FairValue(START, END)
    assert f.sigma == f.min_sigma
    f.on_spot(START, D("80000"))
    f.on_spot(START + timedelta(seconds=1), D("80080"))  # 0.1% in one second
    assert abs(f.sigma - 0.001) < 1e-6


def test_reference_before_window_start_does_not_become_the_start_price():
    f = FairValue(START, END)
    f.on_reference(START - timedelta(minutes=2), D("81160"))
    assert f.start_price is None and f.p_up(START) is None
    f.on_reference(START + timedelta(seconds=1), D("81115"))
    assert f.start_price == D("81115") and f.start_source == "twap-at-start"
    assert f.reference_price == D("81115")
