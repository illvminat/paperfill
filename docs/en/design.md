# paperfill: design notes for clients

A paper-trading harness for Polymarket's short "Up or Down" crypto markets. It never
places live orders. This page summarises the decisions and measurements recorded in
`docs/` (Russian) for readers who only need the engineering picture.

## What it does, end to end

| Stage | Module | Verified by |
|---|---|---|
| Market discovery with each market's own fee schedule, tick and minimum size | `markets.py` | recorded Gamma response, live run |
| Live recording of book snapshots, level changes, prints and three reference price feeds (Binance spot, Chainlink spot, Chainlink 60 s TWAP), gzip, gaps recorded, reconnection with capped backoff, disk cap | `recorder.py`, `collect.py` | wire-format examples from the docs; 6-hour collection of 73 windows |
| Full trade history of a market (cursor walk, `Retry-After` honoured, repeated cursors refused, personal fields dropped) | `history.py` | 5,347 trades of a closed market, deterministic digest |
| Fees from the market's `feeSchedule`: `C × rate × (p(1−p))^exponent`, 5-decimal rounding | `fees.py` | 82 example values from the official fee tables, copied by hand |
| Order book rebuilt from snapshots and `price_change` (verified: 400 of 402 live snapshots matched) | `book.py` | recorded snapshot; empirical check |
| Paper execution: GTC/GTD/FOK/FAK, post-only, tick and size validation, taker level walk, conservative maker fills (only when the market trades *through* the price, once per crossing), long-only portfolio, settlement at 1/0 | `execution.py` | hand-computed references; cash change equals realized P&L |
| Risk envelope: per-order, per-token, per-market and total limits counting resting orders, daily and total loss breakers on equity, pause, file kill switch | `risk.py` | one test per limit; halts seen on real tapes |
| Hash-chained journal with schema version, checkpoints and resume | `journal.py`, `runner.py` | tamper test; halt at event 684 → resume → 244,209 events, chain intact |
| Report computed from the journal only (fill rate, both-sides participation, fees, rebates as an upper bound, realized P&L after all fees, max drawdown sampled at every fill, per-token and per-conviction breakdowns) | `report.py` | per-token P&L adds up to realized P&L |
| Dashboard with run states, journal tail, token-protected kill button, `/healthz` | `dashboard.py` | FastAPI test client |
| Research: parallel batch over many windows (byte-identical to sequential), calibration against the book mid (Brier, reliability), parameter sweep with a time split, pair scan with duration floors | `batch.py`, `calibration.py`, `sweep.py`, `pairs.py` | 68-window batch, 76-window calibration, 32-combination sweep, 76-window pair scan |
| Agent access: MCP server over stdio with structured tools and a report resource | `mcp_server.py` | in-process client tests, stdio smoke |

## What the sample strategies showed

Both strategies are samples of how a strategy plugs in. On 68 consecutive 5-minute
BTC windows recorded overnight (23:03–05:03 UTC), both lose: mean −3.35 and −3.85 per
window on 100 USDC. Calibration explains why: the lognormal fair value is as good as
the book mid in the first minute and three times worse by the fourth (Brier 0.112 vs
0.039), i.e. the market prices the same Chainlink feed faster. A 32-combination sweep
chosen on earlier windows reached −0.06 on train and −1.83 on later test windows. The
harness produced these answers in minutes and without money; that is its purpose.

## Deliberate limits

- One executor, the paper one. Live trading is out of scope by decision, not by omission.
- No latency or queue model: the harness tests logic against a tape, not speed.
- Resting orders queue behind the size already at their price (queue model, a parameter); the queue is an estimate, not the exchange's.
- Kalshi is not integrated: its Developer Agreement limits the API to a member's own trading. A venue interface lets a licensed client plug in their own data.
- Windows are consecutive and not independent; means are descriptions, not expectations. Batch summaries carry a bootstrap interval and a t-statistic, and count halted windows, which are settled and included.
- Resume restores fills and progress from the journal, not resting orders.
- The journal's hash chain is unkeyed: it detects edits, not a full rewrite.

## Sources of truth

Every claim about Polymarket behaviour points at a documentation page listed in
`SOURCES.md` (cached locally, not redistributed). Where the documentation is silent, the
code says so: the rounding mode at the fifth decimal of a fee, the `price_change`
level semantics (verified empirically), the slug convention for the window start.
