# PolyMarket-Bot

**Status:** research scaffold. Read-only ingestion and local paper simulation only; live trading is not implemented or authorized.

## Capabilities

- Export public wallet trades from Polymarket Data API v2 to JSONL.
- Normalize observed trade rows and store them idempotently in SQLite.
- Report observed share volume and gross notional, with explicit warnings that these are not P&L.
- Run deterministic paper simulations against supplied bid/ask snapshots with explicit fees, slippage, depth, inventory, freshness, size, notional, position, cash, and kill-switch checks.
- Run mocked tests, `ruff`, and `mypy --strict` locally and in GitHub Actions CI across Python 3.11–3.13.

No authenticated order submission, transaction signing, fund movement, private-key handling, account-position reconciliation, or live trading is implemented. A public wallet's trades are not a complete accounting ledger.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check src tests
python -m mypy
```

Wallets must be full `0x…` addresses (40 hex digits); malformed values are rejected before any request is made.
To use a different Data API host, set `POLYMARKET_DATA_API_BASE_URL` in the process environment (must be `https`).

Export public observations for a bounded Unix epoch-second window:

```bash
polybot export-trades --wallet 0xPUBLIC_WALLET --start 1799000000 --end 1800000000 --output reports/trades.jsonl
```

Ingest to SQLite (repeated runs deduplicate identical source rows):

```bash
polybot sync-trades --wallet 0xPUBLIC_WALLET --start 1799000000 --end 1800000000 --db data/research.sqlite3
polybot report --wallet 0xPUBLIC_WALLET --db data/research.sqlite3
```

`sync-trades` writes in batches and always closes out its ingestion run (`succeeded`, or `failed` with the error), so `report` shows
recent runs you can trust. `export-trades` and `paper-sim` write via a temp file and rename, so a failure never leaves a truncated output.
`report` exits with an error if the database does not exist rather than creating an empty one.

### Paper simulation

Supply JSONL records with an order intent and its quote snapshot. Each record should contain `timestamp`, `market_id`, `side` (`BUY`/`SELL`), `quantity`, and a nested `quote` with `market_id`, `timestamp`, `bid`, `ask`, `bid_size`, and `ask_size`. Optionally supply `now` as the simulated evaluation timestamp.

```bash
polybot paper-sim --input fixtures/paper-orders.jsonl --output reports/paper-results.jsonl --initial-cash 100 --max-order-size 5 --max-position-size 10 --max-notional 5 --max-quote-age 5 --fee-rate 0.01 --slippage-bps 10
```

The fee rate and slippage are user-supplied assumptions, not assertions about Polymarket's current fee schedule or achievable fills. The simulator uses all-or-none top-of-book fills and does not model queue position, hidden liquidity, latency, or partial fills.

## Architecture and boundaries

- `src/polymarket_bot/data_api.py` — read-only Data API client.
- `src/polymarket_bot/numeric.py` — strict, bounded decimal/int parsing shared by every layer.
- `src/polymarket_bot/models.py` — normalized immutable trade observations.
- `src/polymarket_bot/store.py` — SQLite event store, idempotency, ingestion runs, activity reports.
- `src/polymarket_bot/paper.py` — local paper execution and risk controls.
- `src/polymarket_bot/cli.py` — export, sync, report, and paper-sim commands.
- `docs/ARCHITECTURE.md`, `docs/API_REFERENCE.md`, `docs/CODE_REVIEW.md`, `docs/DEVELOPMENT_LOG.md`, `SECURITY.md`.

Read the [official Data API v2 reference](https://data-api.polymarket.com/v2/docs) before changing endpoint assumptions. No strategy has been shown to have positive expected value. Any future research must include executable prices, fees, spread, depth, slippage, latency, adverse selection, realistic fills, out-of-sample validation, and risk metrics.
