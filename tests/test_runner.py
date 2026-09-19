"""Strategy, runner and report on a synthetic tape with hand-computed results."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.history import TradePrint
from paperfill.journal import Journal, verify
from paperfill.markets import from_gamma_json
from paperfill.report import compute, to_json, to_markdown
from paperfill.risk import RiskLimits
from paperfill.runner import RunConfig, Runner, events_from_recording
from paperfill.strategy import Context, Quote, TwoSidedQuoter

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def market(gamma_market_raw, resolved=False):
    raw = dict(gamma_market_raw)
    if resolved:
        raw["closed"] = True
        raw["umaResolutionStatuses"] = raw.get("umaResolutionStatuses")
        raw["umaResolutionStatus"] = "resolved"
        raw["outcomePrices"] = '["1", "0"]'
    return from_gamma_json(raw)


def clock():
    t = [T0]

    def tick():
        t[0] += timedelta(milliseconds=1)
        return t[0]

    return tick


def test_resolved_fixture_exposes_payouts(gamma_market_raw):
    m = market(gamma_market_raw, resolved=True)
    assert m.resolved and m.payouts == {m.yes_token: D("1"), m.no_token: D("0")}
    assert market(gamma_market_raw).payouts is None


def test_quoter_is_deterministic_and_leans_with_drift(gamma_market_raw):
    m = market(gamma_market_raw)

    def run():
        s = TwoSidedQuoter(size=D("10"), requote_every=timedelta(0), lookback=timedelta(seconds=60))
        out = []
        for i, up in enumerate(["0.50", "0.52", "0.55"]):
            ctx = Context(
                market=m,
                now=T0 + timedelta(seconds=10 * i),
                books={},
                last_prices={m.yes_token: D(up), m.no_token: D("1") - D(up)},
                positions={},
                open_orders=[],
            )
            out.append([a for a in s.on_tick(ctx) if isinstance(a, Quote)])
        return out

    a, b = run(), run()
    assert a == b  # same input, same decisions
    first, last = a[0], a[2]
    assert [q.price for q in first] == [D("0.49"), D("0.49")]  # one tick under the print
    assert [q.size for q in first] == [D("10"), D("10")] and first[0].lean == 0
    # drift +0.05 over the lookback -> lean = min(0.5, 20 * 0.05) = 0.5 -> 15 / 5 shares
    assert last[0].lean == D("0.5") and [q.size for q in last] == [D("15"), D("5")]


def _tape(m):
    up, down = m.yes_token, m.no_token
    t = lambda s: T0 + timedelta(seconds=s)  # noqa: E731
    return [
        TradePrint(t(0), 0, "BUY", D("0.60"), D("50"), up, "Up"),
        TradePrint(t(0), 1, "BUY", D("0.40"), D("50"), down, "Down"),
        # our bids rest at 0.59 (up) and 0.39 (down); prints through them fill us as maker
        TradePrint(t(6), 2, "SELL", D("0.58"), D("3"), up, "Up"),
        TradePrint(t(7), 3, "SELL", D("0.38"), D("3"), down, "Down"),
        TradePrint(t(30), 4, "BUY", D("0.61"), D("1"), up, "Up"),
    ]


def test_runner_end_to_end_with_settlement_and_hand_computed_pnl(gamma_market_raw, tmp_path):
    m = market(gamma_market_raw, resolved=True)
    journal = Journal.open(tmp_path / "journal.jsonl", clock=clock())
    strategy = TwoSidedQuoter(size=D("5"), requote_every=timedelta(seconds=5), quote_ttl=None)
    runner = Runner(m, strategy, journal, RunConfig(capital=D("100")), mode="replay")
    result = runner.run(_tape(m), payouts=m.payouts)
    journal.close()
    assert result.events == 5 and result.settled and result.halted is None
    # two maker fills: 3 Up @ 0.59 = 1.77, 3 Down @ 0.39 = 1.17; no fees as maker
    assert [(f.price, f.size, f.liquidity) for f in result.fills] == [
        (D("0.59"), D("3"), "maker"),
        (D("0.39"), D("3"), "maker"),
    ]
    # settlement: Up pays 1 -> 3.00; Down pays 0. P&L = 3.00 - 1.77 - 1.17 = 0.06
    assert result.portfolio.realized_pnl == D("0.06")
    assert result.portfolio.cash == D("100") - D("1.77") - D("1.17") + D("3")
    v = verify(journal.entries)
    assert v.ok
    kinds = [e.kind for e in journal.entries]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "settle" in kinds
    assert kinds.count("fill") == 2 and kinds.count("order_submitted") >= 2

    metrics = compute(journal.entries)
    assert metrics.fills == 2 and metrics.maker_fills == 2 and metrics.both_sides
    assert metrics.realized_pnl == D("0.06") and metrics.fees == 0
    assert metrics.per_token[m.yes_token]["settled_pnl"] == "1.230000"  # 3 - 1.77 (maker, no fee)
    assert metrics.per_token[m.no_token]["settled_pnl"] == "-1.170000"
    md = to_markdown(metrics)
    assert "Realized P&L after fees | **0.0600**" in md and "Both sides filled | yes" in md
    assert D(json.loads(to_json(metrics))["realized_pnl"]) == D("0.06")


def test_risk_halt_cancels_and_blocks(gamma_market_raw, tmp_path):
    m = market(gamma_market_raw)
    journal = Journal(clock=clock())
    strategy = TwoSidedQuoter(size=D("5"), requote_every=timedelta(seconds=5), quote_ttl=None)
    cfg = RunConfig(capital=D("100"), limits=RiskLimits(total_loss_limit=D("0.5")))
    runner = Runner(m, strategy, journal, cfg, mode="replay")
    up = m.yes_token
    t = lambda s: T0 + timedelta(seconds=s)  # noqa: E731
    tape = [
        TradePrint(t(0), 0, "BUY", D("0.60"), D("50"), up, "Up"),
        TradePrint(t(6), 1, "SELL", D("0.58"), D("5"), up, "Up"),  # we get 5 @ 0.59 as maker
        TradePrint(t(12), 2, "SELL", D("0.40"), D("5"), up, "Up"),  # mark drops: equity -0.95
        TradePrint(t(20), 3, "BUY", D("0.41"), D("5"), up, "Up"),
    ]
    result = runner.run(tape, payouts={up: D("1"), m.no_token: D("0")})
    assert result.halted and result.halted.startswith("total loss limit")
    assert result.settled  # a breaker halt is a final outcome: it settles and counts
    kinds = [e.kind for e in journal.entries]
    assert "risk_halt" in kinds
    assert not any(e.kind == "order_submitted" for e in journal.entries[kinds.index("risk_halt") :])


def test_events_from_recording_maps_all_kinds(tmp_path):
    rec = tmp_path / "r.jsonl"
    rec.write_text(
        "\n".join(
            [
                '{"kind": "start", "recv_ts": "2026-09-19T00:00:00+00:00"}',
                '{"kind": "book", "recv_ts": "2026-09-19T00:00:01+00:00", '
                '"payload": {"token_id": "t", "bids": [{"price": "0.5", "size": "1"}], '
                '"asks": []}}',
                '{"kind": "price_change", "recv_ts": "2026-09-19T00:00:02+00:00", '
                '"payload": {"price_changes": '
                '[{"token_id": "t", "price": "0.51", "size": "2", "side": "BUY"}]}}',
                '{"kind": "last_trade_price", "recv_ts": "2026-09-19T00:00:03+00:00", '
                '"payload": {"token_id": "t", "price": "0.52", "size": "3", "side": "BUY"}}',
                '{"kind": "gap", "recv_ts": "2026-09-19T00:00:04+00:00", "reason": "x"}',
                '{"kind": "stop", "recv_ts": "2026-09-19T00:00:05+00:00"}',
            ]
        )
        + "\n"
    )
    kinds = [type(e).__name__ for e in events_from_recording(rec)]
    assert kinds == ["BookEvent", "PriceChangeEvent", "TradePrint", "GapEvent"]


def test_price_events_feed_fair_value_and_are_journaled(gamma_market_raw):
    from paperfill.runner import PriceEvent
    from paperfill.strategy import FairValueQuoter

    m = market(gamma_market_raw)
    assert m.window_start == datetime(2026, 9, 18, 21, 50, tzinfo=UTC)
    journal = Journal(clock=clock())
    runner = Runner(
        m,
        FairValueQuoter(size=D("5"), requote_every=timedelta(0)),
        journal,
        RunConfig(),
        mode="record",
    )
    t = lambda s: m.window_start + timedelta(seconds=s)  # noqa: E731
    up = m.yes_token
    events = [
        PriceEvent(t(0), "chainlink.twap", "btc/usd", D("80000")),
        PriceEvent(t(0), "binance", "btcusdt", D("80000")),
        PriceEvent(t(1), "binance", "btcusdt", D("80080")),
        TradePrint(t(2), 0, "BUY", D("0.50"), D("10"), up, "Up"),
        PriceEvent(t(3), "chainlink.twap", "btc/usd", D("80200")),  # reference well above start
        TradePrint(t(4), 1, "BUY", D("0.50"), D("10"), up, "Up"),
    ]
    runner.run(events)
    kinds = [e.kind for e in journal.entries]
    assert "fair_value" in kinds and kinds.count("order_submitted") >= 1
    fv = next(e for e in journal.entries if e.kind == "fair_value")
    assert fv.data["start_price"] == "80000" and fv.data["start_source"] == "twap-at-start"
    submitted = [e for e in journal.entries if e.kind == "order_submitted"]
    assert all(D(e.data["lean"]) > 0 for e in submitted)  # fair value above the 0.50 print


def test_report_flags_engine_mismatch_from_a_forged_run_end(gamma_market_raw, tmp_path):
    m = market(gamma_market_raw, resolved=True)
    journal = Journal.open(tmp_path / "journal.jsonl", clock=clock())
    strategy = TwoSidedQuoter(size=D("5"), requote_every=timedelta(seconds=5), quote_ttl=None)
    Runner(m, strategy, journal, RunConfig(capital=D("100")), mode="replay").run(
        _tape(m), payouts=m.payouts
    )
    journal.close()
    entries = journal.entries
    assert compute(entries).engine_mismatch == []
    # forge the run_end totals: the report must notice, the chain aside
    forged = [
        e
        if e.kind != "run_end"
        else type(e)(
            e.seq, e.ts, e.kind, {**e.data, "realized_pnl": "9.99", "fees": "1"}, e.prev, e.hash
        )
        for e in entries
    ]
    mm = compute(forged)
    assert any("fees" in x for x in mm.engine_mismatch)
    assert any("realized P&L" in x for x in mm.engine_mismatch)
    assert "Engine mismatch" in to_markdown(mm)
