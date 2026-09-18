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
