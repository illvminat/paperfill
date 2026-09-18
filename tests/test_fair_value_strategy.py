from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from paperfill.book import OrderBook
from paperfill.markets import from_gamma_json
from paperfill.strategy import Context, FairValueQuoter, Quote

T0 = datetime(2026, 9, 18, 21, 51, tzinfo=UTC)


def _ctx(m, fair_up, up_bid="0.60", up_ask="0.62", down_bid="0.38", down_ask="0.40"):
    def book(token, bid, ask):
        return OrderBook.from_snapshot(
            {
                "token_id": token,
                "bids": [{"price": bid, "size": "100"}],
                "asks": [{"price": ask, "size": "100"}],
            }
        )

    return Context(
        market=m,
        now=T0,
        books={
            m.yes_token: book(m.yes_token, up_bid, up_ask),
            m.no_token: book(m.no_token, down_bid, down_ask),
        },
        last_prices={},
        positions={},
        open_orders=[],
        fair_up=None if fair_up is None else D(fair_up),
    )


def test_no_fair_value_no_quotes(gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    assert FairValueQuoter(requote_every=timedelta(0)).on_tick(_ctx(m, None)) == []


def test_pair_under_one_dollar_quotes_both_sides_sized_by_edge(gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    s = FairValueQuoter(size=D("10"), edge_gain=D("10"), requote_every=timedelta(0))
    quotes = [a for a in s.on_tick(_ctx(m, "0.71")) if isinstance(a, Quote)]
    # up: mid 0.61, fair 0.71 -> edge +0.10 -> factor 2.0 -> 20 shares at the best bid 0.60
    # down: mid 0.39, fair 0.29 -> edge -0.10 -> factor 0 -> below min size, skipped
    assert [(q.token_id == m.yes_token, q.price, q.size, q.lean) for q in quotes] == [
        (True, D("0.60"), D("20.00"), D("0.10"))
    ]


def test_pair_over_one_dollar_quotes_only_positive_edge_above_threshold(gamma_market_raw):
    m = from_gamma_json(gamma_market_raw)
    s = FairValueQuoter(size=D("10"), min_edge=D("0.02"), requote_every=timedelta(0))
    ctx = _ctx(m, "0.62", up_bid="0.61", up_ask="0.63", down_bid="0.40", down_ask="0.42")
    quotes = [a for a in s.on_tick(ctx) if isinstance(a, Quote)]
    # bids sum to 1.01 > 1: only sides with edge >= 0.02. up edge = 0.62 - 0.62 = 0 -> skip;
    # down edge = 0.38 - 0.41 = -0.03 -> skip
    assert quotes == []
    ctx = _ctx(m, "0.70", up_bid="0.61", up_ask="0.63", down_bid="0.40", down_ask="0.42")
    quotes = [a for a in s.on_tick(ctx) if isinstance(a, Quote)]
    assert len(quotes) == 1 and quotes[0].token_id == m.yes_token and quotes[0].lean == D("0.08")
