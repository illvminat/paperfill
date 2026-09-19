"""Fair value of an Up/Down market from external reference prices.

The market resolves "Up" if the Chainlink 60-second TWAP at the end of the window is
greater than or equal to the price at the beginning of the window (market
description, tests/fixtures/gamma_market_btc_updown.json; resolution source
`polymarket/realtime-data.md`, Chainlink streams). So the fair probability of "Up" is
the probability that the reference price finishes at or above its start:

    p_up = Phi( ln(P_now / P_start) / (sigma * sqrt(tau)) )

with `sigma` the per-second volatility of log returns estimated by an exponentially
weighted average of recent squared returns and `tau` the seconds left in the window.
This is the textbook driftless lognormal model; it ignores the TWAP smoothing at the
end of the window and jumps. It is a *reference* fair value for a sample strategy,
not a claim of edge. Probabilities are floats; money stays Decimal elsewhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(slots=True)
class FairValue:
    window_start: datetime
    window_end: datetime
    halflife_seconds: float = 60.0
    min_sigma: float = 1e-5  # per second; floors the estimate before any data
    vol_sample_seconds: float = 0.0  # >0: returns are measured over at least this many seconds
    start_price: Decimal | None = None
    start_source: str | None = None  # "twap-at-start" or "first-seen"
    last_ts: datetime | None = None
    last_price: Decimal | None = None
    _var: float = field(default=0.0)
    _n: int = 0

    def on_spot(self, ts: datetime, price: Decimal) -> None:
        """Feed a spot print (Binance) to update the volatility estimate."""
        if self.last_price is not None and self.last_ts is not None:
            dt = (ts - self.last_ts).total_seconds()
            if 0 < dt < self.vol_sample_seconds:
                return  # wait until the sampling interval has elapsed
            if dt > 0:
                r = math.log(float(price) / float(self.last_price))
                per_second = (r * r) / dt
                alpha = 1.0 - 0.5 ** (dt / self.halflife_seconds)
                self._var = (
                    per_second if self._n == 0 else (1 - alpha) * self._var + alpha * per_second
                )
                self._n += 1
        self.last_ts, self.last_price = ts, price

    def on_reference(self, ts: datetime, price: Decimal) -> None:
        """Feed the resolution reference (Chainlink TWAP).

        Values before the window start only update the running reference; the start
        price is the first value at or after the window start. If the feed began
        mid-window (more than 2 s late), that first value is used and flagged.
        """
        if self.start_price is None and ts >= self.window_start:
            self.start_price = price
            late = (ts - self.window_start).total_seconds() > 2
            self.start_source = "first-seen" if late else "twap-at-start"
        self.reference_ts, self.reference_price = ts, price

    reference_ts: datetime | None = None
    reference_price: Decimal | None = None

    @property
    def sigma(self) -> float:
        return max(self.min_sigma, math.sqrt(self._var))

    def p_up(self, now: datetime) -> float | None:
        if self.start_price is None or self.reference_price is None:
            return None
        tau = max(1.0, (self.window_end - now).total_seconds())
        z = math.log(float(self.reference_price) / float(self.start_price)) / (
            self.sigma * math.sqrt(tau)
        )
        return _phi(z)
