from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from paperfill.batch import split_by_time
from paperfill.runner import RunConfig
from paperfill.signals import FairValue
from paperfill.strategy import Cancel, Context, FairValueQuoter
from paperfill.sweep import Combo, combos, config_for, factory

T0 = datetime(2026, 9, 18, 21, 50, tzinfo=UTC)


def test_split_by_time_keeps_order_and_share():
    names = [f"0x{i:064x}-20260918T{h:02d}0000Z.jsonl.gz" for i, h in enumerate([5, 1, 3, 2, 4])]
    train, test = split_by_time([Path(n) for n in names], 0.6)
    assert [p.name.split("-")[-1][9:11] for p in train] == ["01", "02", "03"]
    assert len(train) == 3 and len(test) == 2
    ts = lambda p: p.name.split("-")[-1]  # noqa: E731
    assert max(ts(p) for p in train) < min(ts(p) for p in test)


def test_grid_combos_and_factories_are_picklable():
    import pickle

    grid = {
        "min_edge": ["0.02"],
        "edge_gain": ["10", "5"],
        "shrink_to_mid": ["0"],
        "stop_after_s": [0, 120],
        "vol_sample_seconds": [1.0],
    }
    cs = combos("fair-value", grid)
    assert len(cs) == 4 and cs[0].key.startswith("edge_gain=")
    f = factory(cs[0], "5")
    strategy = pickle.loads(pickle.dumps(f))()
    assert isinstance(strategy, FairValueQuoter) and strategy.size == D("5")
    cfg = config_for(Combo("fair-value", {"vol_sample_seconds": 60.0}), RunConfig())
    assert (
        cfg.vol_sample_seconds == 60.0
        and pickle.loads(pickle.dumps(cfg)).vol_sample_seconds == 60.0
    )


def test_vol_sampling_interval_ignores_faster_prints():
    f = FairValue(T0, T0 + timedelta(minutes=5), vol_sample_seconds=60.0)
    f.on_spot(T0, D("80000"))
    f.on_spot(T0 + timedelta(seconds=1), D("80800"))  # +1% in a second: ignored
    assert f.sigma == f.min_sigma
    f.on_spot(T0 + timedelta(seconds=60), D("80080"))  # +0.1% over a minute
    assert abs(f.sigma - 0.001 / 60**0.5) < 1e-6


def test_shrink_and_stop_after(gamma_market_raw):
    from paperfill.markets import from_gamma_json

    m = from_gamma_json(gamma_market_raw)
    ctx = Context(
        market=m,
        now=m.window_start + timedelta(seconds=130),
        books={},
        last_prices={m.yes_token: D("0.60"), m.no_token: D("0.40")},
        positions={},
        open_orders=[],
        fair_up=D("0.90"),
    )
    stopped = FairValueQuoter(
        stop_after=timedelta(seconds=120), requote_every=timedelta(0)
    ).on_tick(ctx)
    assert stopped == []  # nothing open to cancel, no new quotes
    shrunk = FairValueQuoter(shrink_to_mid=D("0.5"), requote_every=timedelta(0), edge_gain=D("10"))
    quotes = [a for a in shrunk.on_tick(ctx) if not isinstance(a, Cancel)]
    # effective fair = 0.5*0.90 + 0.5*0.60 = 0.75; edge on Up = 0.15 -> factor 2.5
    assert quotes and quotes[0].lean == D("0.15") and quotes[0].size == D("12.50")
