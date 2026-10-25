# PolyMarket-Bot

A Python research scaffold for collecting public Polymarket wallet-trade observations, storing normalized records locally, and evaluating hypothetical orders with a deterministic paper simulator.

> **Status: read-only research and paper simulation.** Live trading is not implemented or authorized. This project does not submit orders, sign transactions, move funds, handle private keys, or reconcile a wallet's authoritative positions and balances.

## What it does

- **Export public trades:** retrieve wallet-trade observations from the Polymarket Data API v2 and write JSON Lines (JSONL).
- **Normalize and store observations:** validate trade fields and persist normalized records in SQLite.
- **Deduplicate repeated ingestion:** use a deterministic fingerprint of each normalized source row to suppress identical rows.
- **Report observed activity:** summarize trade-row counts, share volume, gross notional, and recent ingestion runs.
- **Simulate hypothetical orders:** evaluate supplied order intents against supplied bid/ask snapshots with configurable fee and slippage assumptions.
- **Apply paper risk checks:** check quote freshness, market matching, displayed depth, cash, inventory, binary-token price bounds, order size, position size, notional, and a kill switch.
- **Run quality checks:** use pytest, Ruff, and mypy; GitHub Actions is configured for Python 3.11, 3.12, and 3.13.

These are research and engineering capabilities, not evidence that a trading strategy has an edge or will be profitable.

## Scope and accounting caveats

Public wallet trades are observations, not a complete accounting ledger. The reports' `observed_share_volume` and `observed_gross_notional_usdc` metrics are **not** positions, cash flows, realized P&L, or investment returns. They do not account for all activity such as current balances, settlements, or a wallet's full transaction history.

The paper simulator is intentionally simplified:

- It assumes all-or-none fills at the supplied top ask for BUY orders or top bid for SELL orders, adjusted by the configured slippage.
- It treats displayed top-of-book size as executable depth for the entire simulated order.
- It does not model queue position, hidden liquidity, latency, partial fills, tick-size rounding, or a persisted portfolio.
- Fee rate and slippage are operator-supplied assumptions. They are not claims about Polymarket's current fee schedule or achievable execution prices.

No strategy is presented as profitable. Any future strategy evaluation should account for executable prices, fees, spread, depth, slippage, latency, adverse selection, realistic fills, out-of-sample validation, and drawdown and exposure metrics.

## Requirements

- Python 3.11 or newer
- pip
- Network access to the public Polymarket Data API for trade export and ingestion

The package's runtime dependency is `httpx`. Development dependencies include pytest, pytest-asyncio, Ruff, and mypy.

## Installation

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Check the available commands:

```bash
polybot --help
polybot export-trades --help
polybot sync-trades --help
polybot report --help
polybot paper-sim --help
```

## Configuration

The client uses the public Data API and does not require credentials. By default, it requests:

`https://data-api.polymarket.com/v2/trades`

An optional environment variable, `POLYMARKET_DATA_API_BASE_URL`, overrides the base URL. The configured URL must use HTTPS and must not embed credentials. The CLI reads the process environment directly; it does **not** load `.env` files automatically.

For example, in Bash:

```bash
export POLYMARKET_DATA_API_BASE_URL="https://data-api.polymarket.com/v2"
```

See [`.env.example`](.env.example) for the documented setting. Do not put private keys, seed phrases, or credentials in this project.

Wallet arguments must be full `0x`-prefixed addresses containing 40 hexadecimal digits. Trade-window `--start` and `--end` values are Unix epoch seconds, with `0 < start <= end`. The client uses cursor pagination; it does not use offset pagination for the trade feed.

## Usage

The examples below use placeholder wallet and time-window values. Replace them with a wallet address and a bounded time range you intend to research.

### 1. Export public trade observations

```bash
polybot export-trades \
  --wallet 0xYOUR_PUBLIC_WALLET_ADDRESS \
  --start 1799000000 \
  --end 1800000000 \
  --output reports/trades.jsonl
```

Each output line is a JSON object from the public API. The output is written through a temporary file and renamed on success to avoid leaving a partial file that looks complete.

### 2. Ingest observations into SQLite

```bash
polybot sync-trades \
  --wallet 0xYOUR_PUBLIC_WALLET_ADDRESS \
  --start 1799000000 \
  --end 1800000000 \
  --db data/research.sqlite3
```

The store normalizes valid rows, counts invalid rows, suppresses identical source-row fingerprints, and records ingestion-run status. Re-running the command re-fetches the specified window; ingestion is not yet incremental or resume-based.

### 3. Report observed activity

```bash
polybot report \
  --wallet 0xYOUR_PUBLIC_WALLET_ADDRESS \
  --db data/research.sqlite3
```

Optional `--start` and `--end` arguments restrict the report window. The report includes observed BUY/SELL row counts, observed share volume, observed gross notional, an accounting warning, and recent ingestion-run summaries. It fails if the specified database does not exist rather than silently creating an empty one.

### 4. Run the paper simulator

A sample input fixture is available at [`fixtures/paper-orders.jsonl`](fixtures/paper-orders.jsonl). Run it with explicit limits:

```bash
polybot paper-sim \
  --input fixtures/paper-orders.jsonl \
  --output reports/paper-results.jsonl \
  --initial-cash 100 \
  --max-order-size 5 \
  --max-position-size 10 \
  --max-notional 5 \
  --max-quote-age 5 \
  --fee-rate 0.01 \
  --slippage-bps 10
```

Each non-empty JSONL input record contains an order intent and a nested quote snapshot:

```json
{
  "timestamp": 1800000000,
  "market_id": "token-yes",
  "side": "BUY",
  "quantity": "4",
  "quote": {
    "market_id": "token-yes",
    "timestamp": 1800000000,
    "bid": "0.48",
    "ask": "0.50",
    "bid_size": "20",
    "ask_size": "20"
  }
}
```

Required order fields are `timestamp`, `market_id`, `side` (`BUY` or `SELL`), `quantity`, and `quote`. The quote requires `market_id`, `timestamp`, `bid`, `ask`, `bid_size`, and `ask_size`. An optional `now` field supplies the simulated evaluation timestamp; when omitted, the order timestamp is used.

The simulator writes one result per input record and prints a summary of orders, fills, and rejections. The example's fee and slippage values are illustrative assumptions only. The paper portfolio is in-memory and is not reconciled with a real wallet.

## Architecture

| Path | Responsibility |
| --- | --- |
| `src/polymarket_bot/cli.py` | CLI commands: export, sync, report, and paper simulation |
| `src/polymarket_bot/data_api.py` | Read-only HTTP client, retries, response validation, cursor pagination |
| `src/polymarket_bot/models.py` | Validated, normalized immutable trade observations |
| `src/polymarket_bot/numeric.py` | Bounded decimal and integer parsing for external values |
| `src/polymarket_bot/store.py` | SQLite storage, deduplication, ingestion runs, and activity reports |
| `src/polymarket_bot/paper.py` | Quote snapshots, simulated portfolio, and paper risk controls |
| `tests/` | Mocked API, CLI, storage, normalization, and paper-simulation tests |
| `docs/ARCHITECTURE.md` | System boundaries and implementation sequence |
| `docs/API_REFERENCE.md` | Data API endpoint and pagination notes |
| `docs/CODE_REVIEW.md` | Review findings, fixes, and deferred limitations |
| `docs/DEVELOPMENT_LOG.md` | Development and validation history |
| `SECURITY.md` | Security policy and operating boundaries |

The data path is deliberately separated: public API ingestion feeds normalized observations and SQLite storage; the paper simulator separately consumes operator-supplied quote snapshots and hypothetical order intents. The public trade feed is not used as a substitute for authoritative account positions.

## Development and tests

Install the development dependencies as described above, then run:

```bash
python -m pytest
python -m ruff check src tests
python -m mypy
```

GitHub Actions runs the lint, type-check, and test commands against Python 3.11, 3.12, and 3.13 for pushes to `main` and pull requests. Check the repository's Actions tab for the status of a particular commit; the presence of a workflow does not itself mean a run passed.

## Security and operational boundaries

- This project does not place authenticated orders, sign transactions, approve tokens, transfer funds, or handle private keys.
- Do not commit secrets, wallet exports, personal credentials, or private account data.
- Treat API responses, market metadata, repository content, and third-party code as untrusted input.
- Keep research ingestion, simulation, and any future execution service separated by explicit interfaces.
- Before any live execution is considered, require verified API/SDK behavior, authoritative position and balance reconciliation, idempotent order handling, stale-data checks, strict risk limits, monitoring, an emergency kill switch, paper-trading acceptance criteria, and a security review.

See [`SECURITY.md`](SECURITY.md) for the project's security policy. This README and the code review notes are not a security audit of Polymarket or its infrastructure.

## References

- [Official Polymarket Data API v2 reference](https://data-api.polymarket.com/v2/docs)
- [Polymarket developer documentation](https://docs.polymarket.com/)
- [Project API reference notes](docs/API_REFERENCE.md)
- [Project architecture](docs/ARCHITECTURE.md)
- [MIT License](LICENSE)
