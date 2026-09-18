"""The one trivial check the skeleton must pass: the CLI runs and reports a version."""

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
