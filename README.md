# paperfill

Paper-trading harness for [Polymarket](https://polymarket.com) prediction markets,
built around the short crypto "Up or Down" markets (5 and 15 minutes). It records live
order books, replays full trade histories, simulates fills with each market's own fee
schedule, enforces a risk envelope with a kill switch, journals every decision in a
hash-chained log and produces an honest metrics report.

## What this is not

- **It never places live orders.** There is exactly one executor and it is the paper
  one. That is a legal decision for the author's jurisdiction, not a missing feature:
  see `docs/decisions/0001-resheniya-do-koda.md`.
- **It does not promise profit.** Profitability is a property of a strategy, not of
  this code. The bundled strategy is a sample of how a strategy plugs in; its reports
  on real tapes are negative and are shown as such.
- **It does not scrape polymarket.com.** Only the documented public APIs are used,
  rate limits (`429` / `Retry-After`) are respected, and no data is redistributed.
- **It keeps no personal data.** Wallet addresses and transaction hashes present in
  API responses are dropped before anything is written to disk.

## Requirements

- Python 3.11 or newer (3.11 and 3.14 are tested in CI), or Docker.
- No API key, wallet or account. Public endpoints only.
- Linux and macOS. Windows is not tested and not claimed.

## Install

```sh
uv sync --all-groups
uv run paperfill --help
```

Or with Docker:

```sh
docker build -t paperfill .
docker run --rm -v "$PWD/data:/app/data" paperfill discover --asset BTC --window 5m
```

The image runs as uid 1000; if your host user differs, pass `--user "$(id -u)"` so
files written into `data/` stay yours. `docker compose up` with `RUN_DIR=<run dir
name>` serves the dashboard of that run on 127.0.0.1:8765.

## Commands

| Command | What it does |
|---|---|
| `paperfill discover --asset BTC --window 5m` | open Up/Down markets, soonest first, with tick, minimum size, fee rate and rebate read from the market itself |
| `paperfill record --condition 0x… --seconds 300` | record the market WebSocket stream (book snapshots, level changes, prints) plus the Binance spot, Chainlink spot and Chainlink 60 s TWAP reference prices for the asset, to an append-only file; gaps are recorded, reconnection backs off automatically (`--no-prices` to skip the price feeds) |
| `paperfill replay --condition 0x…` | download the full trade history of a market (cursor walk, `Retry-After` honoured), store it without personal fields, print VWAPs and a deterministic digest |
| `paperfill run --condition 0x… [--recording file] [--strategy two-sided\|fair-value]` | paper-trade a sample strategy over the trade history (replay) or over a recording; writes `journal.jsonl`, `report.md`, `report.json` into `data/runs/<id>/` |
| `paperfill report <run-dir>` | rebuild the report from the journal (fails if the chain is broken) |
| `paperfill journal verify <file>` | verify the SHA-256 hash chain of a journal |
| `paperfill kill <run-dir>` | raise the kill switch: the next event halts the run and cancels everything |
| `paperfill dashboard <run-dir>` | serve a page with run state, summary, positions, journal tail and a kill button (bound to 127.0.0.1 by default) |
| `paperfill version` | print the installed version |

`run --recording` still asks the Gamma API for the market's parameters (tick, fees,
outcome); the tape itself is read locally. A recording that does not reach the
market's end time is marked to market at its last mark instead of being settled.

Run parameters: `--capital`, `--size`, `--max-order`, `--max-position`, `--max-market`,
`--max-exposure`, `--daily-loss`, `--total-loss`, or a TOML file via `--config`
(`src/paperfill/config.py` documents the sections; a flag on the command line wins
over the file). `run --resume <run-dir>` continues a halted or interrupted run from
its journal after you remove the `KILL` file: fills and progress are rebuilt from the
journal, open orders are not (they were cancelled on halt).

Research over many windows: `collect` records consecutive windows for hours,
`batch --workers N` runs one strategy over every recording in parallel (results are
byte-identical to a sequential run), `calibrate` scores the fair-value model against
the book mid (Brier, reliability), `sweep` tries a parameter grid on earlier windows
and reports the chosen combination on later ones. Batch summaries include every window
(halted runs are settled and counted), a bootstrap 95% interval of the mean and a
t-statistic; windows are consecutive, so both understate the uncertainty.

Files carry a schema version (`start` record of a recording, `run_start` entry of a
journal); readers refuse newer schemas instead of misreading them. `record --max-mb`
caps the file size. Logs are JSON lines on stderr (`--log-level`); the dashboard
answers `/healthz`.

## As an MCP server

`paperfill mcp` (extra: `uv sync --extra mcp`) exposes the same paper-only tools to an
agent over stdio: `discover_markets`, `fetch_history`, `run_paper`, `read_report`,
`verify_journal`, `list_recordings`, `list_runs`, `run_batch`, `calibrate`,
`raise_kill_switch`, plus a `paperfill://runs/{name}/report.md` resource. Tools return
structured content with schemas and carry read-only / destructive annotations. For
Claude Code: `claude mcp add paperfill -- uv run --directory <repo> paperfill mcp`.

## Sample strategies

Both are samples of how a strategy plugs in, not advice, and both only ever post
resting bids (post-only, GTD).

- `two-sided`: bid both outcome tokens at the best bid, sizes leaned by the recent
  drift of the Up mid.
- `fair-value`: bid both tokens when a pair costs at most one dollar, sized by the
  edge between the book and an external fair value. The fair value follows the
  market's own resolution rule: "Up" if the Chainlink 60 s TWAP at the end of the
  window is at or above the price at its start, so `p_up = Φ(ln(P_now/P_start) /
  (σ√τ))` with `σ` an exponentially weighted volatility of Binance spot returns and
  `τ` the seconds left (`src/paperfill/signals.py`). It needs a recording that
  includes the reference prices; without one it does nothing and the journal says so.

## How fills are simulated

The model is deliberately conservative and documented in `src/paperfill/execution.py`
and `docs/decisions/0002-model-ispolneniya.md`:

- An order that crosses the book is filled level by level as a **taker** and pays
  the taker fee `C × rate × (p × (1 − p)) ^ exponent` from the market's `feeSchedule`,
  rounded to 5 decimals (fees verified against the 83 example values in the official
  fee tables).
- A resting order is filled as a **maker** (no fee; rebate reported separately as an
  upper-bound estimate) only when the market trades *through* its price: a print
  strictly better than the order price in replay, or the opposite side of the book
  crossing it on a recording, once per crossing. Prints exactly at the order price
  are not fills, because queue position is unknown. **Conservative on price, not on
  size:** when a fill happens, its size is the whole print or the whole crossing
  level, with no queue ahead of the order. On thin books this overstates fills.
- Positions are held long-only and settled at the market's resolution payouts
  (1 or 0 per share) when the market is resolved. Buy fees are part of the cost
  basis, so "realized P&L after fees" is after all fees on both legs.
- No latency or queue model: the harness measures a strategy's logic against a tape,
  not its speed.

Level semantics of the `price_change` stream were verified empirically: 400 of 402
book snapshots in a live recording matched the book rebuilt from the preceding
changes (`docs/measurements/2026-09-19-price-change-semantics.md`).

## Risk envelope

Per-order notional, per-token position, per-market cost, total exposure, daily and
total loss breakers (measured on equity at mid), pause, and a file-based kill switch
that a dashboard or an operator can raise from outside the process. Every block and
every halt is written to the journal with its reason.

## Journal and report

Every order, fill, rejection, risk decision and lifecycle event is a journal entry
linked by a SHA-256 hash chain; `journal verify` reports the first edited or deleted
line. The chain is unkeyed: it catches edits, not a full rewrite, and a truncated tail
is only visible through the missing `run_end` entry. Publish the head hash somewhere
else if you need more than that. The
report is computed from the journal only, so it can be regenerated later and never
says anything that was not recorded: orders, fills, fill rate, both-sides
participation, fees, rebates, realized P&L after fees, max drawdown, per-token and
per-lean-bucket breakdowns with win rates once settled.

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov -m "not live"     # offline suite, runs in CI
uv run pytest -m live                 # talks to the public API
make sources-check                    # warns about stale reference sources
```

English design notes for clients: `docs/en/design.md`. Sources of truth for every
claim about Polymarket behaviour are listed in `SOURCES.md`. A claim without a source is marked as unverified. Decisions live in
`docs/decisions/`, requirements in `docs/zadanie.md`, measurements in
`docs/measurements/`.

## License

MIT, see `LICENSE`.
