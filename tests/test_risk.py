from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.execution import Fill, Portfolio
from paperfill.risk import Intent, Mode, RiskEngine, RiskLimits

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
UP, DOWN, M = "up", "down", "0xmarket"


def engine(cash="100", **limits) -> RiskEngine:
    e = RiskEngine(RiskLimits(**{k: D(v) for k, v in limits.items()}), Portfolio(cash=D(cash)))
    e.token_market = {UP: M, DOWN: M}
    e.start({}, T0)
    return e


def buy(token, price, size, fee="0"):
    return Fill("o", token, "BUY", D(price), D(size), D(fee), D("0"), "taker", T0)


def test_limits_each_produce_a_reason():
    e = engine(
        max_order_notional="10",
        max_position_shares="30",
        max_market_notional="20",
        max_total_exposure="25",
    )
    ok = e.check(Intent(M, UP, "BUY", D("0.5"), D("10")), {}, D("0"))
    assert ok.allowed and ok.reason is None
    assert "order notional" in e.check(Intent(M, UP, "BUY", D("0.5"), D("30")), {}, D("0")).reason
    e.portfolio.apply(buy(UP, "0.5", "25"))
    assert "shares" in e.check(Intent(M, UP, "BUY", D("0.2"), D("10")), {}, D("0")).reason
    assert (
        "market notional" in e.check(Intent(M, DOWN, "BUY", D("0.5"), D("20")), {}, D("0")).reason
    )
    d = e.check(Intent(M, DOWN, "BUY", D("0.5"), D("10")), {UP: D("0.5")}, D("10"))
    assert "total exposure" in d.reason  # 12.5 held + 10 open + 5 new = 27.5 > 25
    assert "no shorting" in e.check(Intent(M, DOWN, "SELL", D("0.5"), D("1")), {}, D("0")).reason
    assert e.check(Intent(M, UP, "SELL", D("0.5"), D("5")), {}, D("0")).allowed


def test_daily_and_total_breakers_halt_and_block():
    e = engine(daily_loss_limit="5", total_loss_limit="8")
    e.portfolio.apply(buy(UP, "0.5", "20"))  # cash 90, 20 shares
    assert e.evaluate({UP: D("0.5")}, T0) is None  # equity 100
    reason = e.evaluate({UP: D("0.24")}, T0)  # equity 94.8 -> daily drop 5.2 >= 5
    assert reason.startswith("daily loss limit") and e.mode is Mode.HALTED
    assert "halted" in e.check(Intent(M, UP, "BUY", D("0.5"), D("1")), {}, D("0")).reason
    e2 = engine(daily_loss_limit="50", total_loss_limit="8")
    e2.portfolio.apply(buy(UP, "0.5", "20"))
    e2.evaluate({UP: D("0.5")}, T0 + timedelta(days=1))  # new day starts at equity 100
    assert e2.evaluate({UP: D("0.1")}, T0 + timedelta(days=1)).startswith("total loss limit")


def test_pause_resume_and_kill_file(tmp_path):
    e = engine()
    e.pause()
    assert "paused" in e.check(Intent(M, UP, "BUY", D("0.5"), D("1")), {}, D("0")).reason
    e.resume()
    assert e.check(Intent(M, UP, "BUY", D("0.5"), D("1")), {}, D("0")).allowed
    e.kill_file = tmp_path / "KILL"
    assert e.evaluate({}, T0) is None
    e.kill_file.write_text("stop")
    assert e.evaluate({}, T0) == "kill switch file present" and e.mode is Mode.HALTED
    e.resume()  # resume does not lift a halt
    assert e.mode is Mode.HALTED
