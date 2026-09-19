from datetime import timedelta

from paperfill.calibration import Sample, calibrate, sample_window, to_markdown
from paperfill.markets import from_gamma_json
from paperfill.recorder import JsonlSink


def test_brier_and_reliability_by_hand():
    s = [
        Sample("a", 60, 0.9, 0.7, True),
        Sample("b", 60, 0.8, 0.6, False),
        Sample("c", 60, 0.1, 0.3, False),
        Sample("d", 60, None, 0.5, True),  # no model value: counted for the mid only
    ]
    cal = calibrate(s, [60])
    # model: ((0.9-1)^2 + (0.8-0)^2 + (0.1-0)^2) / 3 = (0.01 + 0.64 + 0.01) / 3 = 0.22
    assert abs(cal.brier_model[60] - 0.22) < 1e-12
    # mid: ((0.7-1)^2 + (0.6)^2 + (0.3)^2 + (0.5-1)^2) / 4 = (0.09+0.36+0.09+0.25)/4 = 0.1975
    assert abs(cal.brier_mid[60] - 0.1975) < 1e-12
    assert cal.windows == 4 and cal.up_rate == 0.5
    top = next(r for r in cal.reliability[60] if r["bin"] == "0.8-1.0")
    assert top["n"] == 2 and abs(top["predicted"] - 0.85) < 1e-12 and top["observed"] == 0.5
    assert "| 60 | 0.220 | 0.197 |" in to_markdown(cal) or "| 60 | 0.220 | 0.198 |" in to_markdown(
        cal
    )


def test_sample_window_reads_model_and_mid_at_offsets(tmp_path, gamma_market_raw):
    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["0", "1"]'
    m = from_gamma_json(raw)
    rec = tmp_path / f"{m.condition_id}-20260918T215000Z.jsonl.gz"
    sink = JsonlSink(rec)
    t = lambda s: (m.window_start + timedelta(seconds=s)).isoformat()  # noqa: E731
    sink.write(
        {
            "kind": "prices.crypto.chainlink.twap",
            "recv_ts": t(0),
            "payload": {
                "symbol": "btc/usd",
                "value": "80000",
                "timestamp": 0,
                "window_seconds": 60,
            },
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.binance",
            "recv_ts": t(0),
            "payload": {"symbol": "btcusdt", "value": "80000", "timestamp": 0},
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.binance",
            "recv_ts": t(1),
            "payload": {"symbol": "btcusdt", "value": "80080", "timestamp": 1},
        }
    )
    sink.write(
        {
            "kind": "book",
            "recv_ts": t(2),
            "payload": {
                "token_id": m.yes_token,
                "bids": [{"price": "0.40", "size": "1"}],
                "asks": [{"price": "0.44", "size": "1"}],
            },
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.chainlink.twap",
            "recv_ts": t(30),
            "payload": {
                "symbol": "btc/usd",
                "value": "79900",
                "timestamp": 30,
                "window_seconds": 60,
            },
        }
    )
    sink.write(
        {
            "kind": "prices.crypto.chainlink.twap",
            "recv_ts": t(61),
            "payload": {
                "symbol": "btc/usd",
                "value": "79900",
                "timestamp": 61,
                "window_seconds": 60,
            },
        }
    )
    sink.close()
    out = sample_window(rec, m, [30, 60])
    assert [s.offset for s in out] == [30, 60]
    assert all(not s.outcome_up for s in out) and all(s.mid_up == 0.42 for s in out)
    assert out[0].model_p_up is not None and out[0].model_p_up < 0.5  # reference fell below start
    assert sample_window(rec, from_gamma_json(gamma_market_raw), [30]) == []  # unresolved: nothing


def test_save_writes_three_files(tmp_path):
    from paperfill.calibration import save

    cal = calibrate([Sample("a", 60, 0.5, 0.5, True)], [60])
    save(cal, [Sample("a", 60, 0.5, 0.5, True)], tmp_path / "cal")
    assert {p.name for p in (tmp_path / "cal").iterdir()} == {
        "calibration.json",
        "calibration.md",
        "samples.jsonl",
    }
