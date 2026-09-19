"""The one trivial check the skeleton must pass: the CLI runs and reports a version."""

import json

from paperfill import __version__
from paperfill.cli import main


def test_version_command_prints_installed_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_version_is_not_the_unknown_fallback():
    # A bare checkout without installation would report "0+unknown"; uv sync installs
    # the package in editable mode, so the real version must be visible.
    assert __version__ == "0.1.0"


def test_discover_prints_table_from_injected_source(capsys, gamma_market_raw):
    from polymarket.models.gamma.market import Market as SdkMarket

    class Page:
        items = (SdkMarket.model_validate(gamma_market_raw),)

    class Source:
        closed = False

        def list_markets(self, **params):
            return [Page()]

        def close(self):
            self.closed = True

    src = Source()
    assert main(["discover", "--asset", "BTC", "--window", "5m"], source_factory=lambda: src) == 0
    out = capsys.readouterr().out
    assert "BTC    5m" in out and gamma_market_raw["conditionId"] in out and "1 market(s)" in out
    assert src.closed


def test_replay_fetches_saves_then_uses_cache(capsys, tmp_path):
    from polymarket.models.data.activity import Trade as SdkTrade

    cid = "0x" + "11" * 32
    rows = [
        {"proxy_wallet": "0x" + "ab" * 20, "side": "BUY", "token_id": "7", "condition_id": cid,
         "size": "5", "price": "0.6", "timestamp": 1000, "transaction_hash": "0x" + "cd" * 32,
         "outcome": "Up"},
        {"proxy_wallet": "0x" + "ab" * 20, "side": "SELL", "token_id": "7", "condition_id": cid,
         "size": "5", "price": "0.4", "timestamp": 999, "transaction_hash": "0x" + "cd" * 32,
         "outcome": "Up"},
    ]  # fmt: skip

    class Page:
        items = tuple(SdkTrade.model_validate(r) for r in rows)
        has_more = False
        next_cursor = None

    class Paginator:
        def first_page(self):
            return Page()

    calls = []

    class Source:
        def list_trades(self, **params):
            calls.append(params)
            return Paginator()

    argv = ["replay", "--condition", cid, "--data-dir", str(tmp_path)]
    assert main(argv, source_factory=Source) == 0
    out = capsys.readouterr().out
    assert "saved:" in out and "2 trades" in out and "vwap 0.5000" in out and "digest:" in out
    assert main(argv, source_factory=Source) == 0
    assert "cached:" in capsys.readouterr().out
    assert len(calls) == 1  # second run did not touch the network


def _fake_market_source(raw, trades_rows=()):
    from polymarket.models.data.activity import Trade as SdkTrade
    from polymarket.models.gamma.market import Market as SdkMarket

    class Page:
        def __init__(self, items, has_more=False):
            self.items, self.has_more, self.next_cursor = tuple(items), has_more, None

    class Paginator:
        def __init__(self, items):
            self._items = items

        def first_page(self):
            return Page(self._items)

    class Source:
        def list_markets(self, **params):
            if params.get("closed") is False and raw.get("closed"):
                return Paginator(())
            return Paginator((SdkMarket.model_validate(raw),))

        def list_trades(self, **params):
            return Paginator(tuple(SdkTrade.model_validate(r) for r in trades_rows))

        def close(self):
            pass

    return Source


def test_run_report_verify_and_kill_roundtrip(capsys, tmp_path, gamma_market_raw):
    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    up = raw["conditionId"]
    rows = [
        {"proxy_wallet": "0x" + "ab" * 20, "side": "BUY", "token_id": t, "condition_id": up,
         "size": "50", "price": p, "timestamp": ts, "transaction_hash": "0x" + "cd" * 32}
        for t, p, ts in (
            (json.loads(raw["clobTokenIds"])[0], "0.60", 1000),
            (json.loads(raw["clobTokenIds"])[1], "0.40", 1000),
        )
    ]  # fmt: skip
    Source = _fake_market_source(raw, rows)
    runs, hist = tmp_path / "runs", tmp_path / "hist"
    argv = ["run", "--condition", up, "--runs-dir", str(runs), "--history-dir", str(hist)]
    assert main(argv, source_factory=Source) == 0
    out = capsys.readouterr().out
    assert "[replay]" in out and "events 2" in out and "settled yes" in out
    run_dir = next(runs.iterdir())
    assert (run_dir / "journal.jsonl").exists() and (run_dir / "report.md").exists()
    assert main(["journal", "verify", str(run_dir / "journal.jsonl")]) == 0
    assert "chain intact" in capsys.readouterr().out
    assert main(["report", str(run_dir)]) == 0
    assert "# paperfill run report" in capsys.readouterr().out
    assert main(["kill", str(run_dir)]) == 0
    assert (run_dir / "KILL").exists()
    # a second run in that directory would halt immediately: verified through the risk engine
    lines = (run_dir / "journal.jsonl").read_text().splitlines()
    (run_dir / "journal.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert main(["journal", "verify", str(run_dir / "journal.jsonl")]) == 1
    assert main(["report", str(run_dir)]) == 1


def test_run_unknown_market_exits_1(capsys, tmp_path):
    class Source:
        def list_markets(self, **params):
            class P:
                items = ()

                def first_page(self):
                    return self

            return P()

        def close(self):
            pass

    assert (
        main(["run", "--condition", "0xnope", "--runs-dir", str(tmp_path)], source_factory=Source)
        == 1
    )


def test_batch_sweep_calibrate_refuse_an_empty_directory(capsys, tmp_path):
    for argv in (
        ["batch", "--recordings", str(tmp_path)],
        ["sweep", "--recordings", str(tmp_path)],
        ["calibrate", "--recordings", str(tmp_path)],
        ["batch", "--recordings", str(tmp_path / "missing")],
    ):
        assert main(argv, source_factory=lambda: None) == 1
        assert "no recordings" in capsys.readouterr().out
