# PolyMarket-Bot

**Status:** early research scaffold. The first vertical slice is read-only market-data ingestion; live trading is not implemented or authorized.

## Current capabilities

- Read public wallet trade observations from Polymarket Data API v2.
- Use opaque cursor pagination with repeated time/window filters and bounded retry behavior.
- Export raw observations to JSONL for reproducible offline research.
- Test API behavior using mocked HTTP responses; tests do not require network access.

This scaffold does **not** submit orders, sign transactions, move funds, reconcile an account's complete positions, calculate realized P&L, estimate true market probabilities, or demonstrate a profitable trading edge. A public wallet's trade history is not a complete accounting ledger.

## Quick start

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
python -m pytest
```

Export public trades for a bounded Unix epoch-second window:

```bash
polybot export-trades \
  --wallet 0xYOUR_PUBLIC_WALLET \
  --start 1799000000 \
  --end 1800000000 \
  --output reports/wallet-trades.jsonl
```

The CLI writes one raw JSON object per line. It may create parent directories for the chosen output path. Use a public wallet you are authorized to research; avoid unnecessarily broad date ranges.

## Repository map

- `src/polymarket_bot/data_api.py` — read-only public Data API client.
- `src/polymarket_bot/cli.py` — bounded JSONL export CLI.
- `tests/` — deterministic mocked API tests.
- `docs/ARCHITECTURE.md` — proposed subsystem boundaries and rollout sequence.
- `docs/API_REFERENCE.md` — official references and API-contract notes.
- `docs/DEVELOPMENT_LOG.md` — decisions, changes, validation, and limitations.
- `SECURITY.md` — credential handling and operating boundaries.

## Development standards

- Start read-only; keep research and any future execution code separated.
- Verify API behavior against the [official Data API v2 reference](https://data-api.polymarket.com/v2/docs).
- Measure realistic executable prices, fees, spread, depth, latency, slippage, and adverse selection before claiming expected value.
- Use out-of-sample tests and report drawdown, turnover, exposure, calibration, and risk-adjusted performance when supported by data.
- Never commit credentials. Never add live order execution without explicit authorization, position reconciliation, risk controls, a kill switch, and paper-trading acceptance tests.

See [the architecture plan](docs/ARCHITECTURE.md), [API notes](docs/API_REFERENCE.md), and [development log](docs/DEVELOPMENT_LOG.md).
