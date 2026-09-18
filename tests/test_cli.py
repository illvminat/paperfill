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
