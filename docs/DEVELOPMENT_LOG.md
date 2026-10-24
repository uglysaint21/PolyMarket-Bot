# Development log

This file records meaningful repository changes, decisions, validation, and known limitations. Add an entry for each substantial PR.

## 2026-10-24 — Initial repository foundation (PR #1)

- Confirmed the connected GitHub integration can create branches, write files, and open pull requests.
- Added a minimal Python 3.11+ package with a read-only Polymarket Data API v2 client and a JSONL export CLI.
- Added mock-based tests for cursor pagination, repeated query filters, malformed responses, and invalid time windows.
- Added CI for Python 3.11, 3.12, and 3.13, plus architecture, API-reference, and security documentation.
- Trading, order submission, signing, transfers, and private-key handling remain out of scope.
- Validation for this repository revision: run `python -m pip install -e '.[dev]'`, `python -m compileall -q src tests`, and `python -m pytest`. CI results must be checked after GitHub runs; no remote run is assumed successful until observed.

## Known limitations

- The client exports raw public rows; it does not yet normalize them, persist them, reconcile positions, or calculate P&L.
- No live API smoke test has been performed in CI or as part of this initial foundation.
- API response contracts and rate limits must be re-verified against the official documentation before production use.
