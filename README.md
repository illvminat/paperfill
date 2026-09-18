# paperfill

Paper-trading harness for [Polymarket](https://polymarket.com) prediction markets.
It replays public trade history and live order books, simulates fills with the
market's own fee schedule, enforces a risk envelope, journals every decision and
produces an honest metrics report.

**Status: skeleton.** Nothing trades yet. Milestones are listed in `docs/zadanie.md`.

## What this is not

- **It never places live orders.** There is exactly one executor, and it is the paper
  one. This is a legal decision, not a missing feature: see
  `docs/decisions/0001-resheniya-do-koda.md`.
- **It does not promise profit.** Profitability is a property of a strategy, not of
  this code. Reports include losing runs.
- **It does not scrape polymarket.com.** Only the documented public APIs are used,
  with their rate limits respected. Data is not redistributed or resold.

## Requirements

- Python 3.11 or newer (3.11 and 3.14 are tested in CI), or Docker.
- No API key, wallet or account. Public endpoints only.
- Linux and macOS. Windows is not tested and not claimed.

## Install

```sh
uv sync --all-groups
uv run paperfill version
```

Or with Docker:

```sh
docker build -t paperfill .
docker run --rm paperfill version
```

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest --cov
```

Sources of truth for every claim about Polymarket behaviour are listed in
`SOURCES.md`. A claim without a source is marked as unverified.

## License

MIT, see `LICENSE`.
