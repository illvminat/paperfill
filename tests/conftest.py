import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def gamma_market_raw() -> dict:
    return json.loads((FIXTURES / "gamma_market_btc_updown.json").read_text())


@pytest.fixture
def clob_book_raw() -> dict:
    return json.loads((FIXTURES / "clob_book_btc_updown.json").read_text())


@pytest.fixture
def trades_raw() -> dict:
    return json.loads((FIXTURES / "data_trades_sample.json").read_text())
