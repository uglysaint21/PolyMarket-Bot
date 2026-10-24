# Development log

Record each substantial change, decisions, validation, and known limitations here.

## 2026-10-24 — Initial repository foundation

- Added Python 3.11+ package, read-only Data API v2 client, JSONL export CLI, mocked API tests, CI, and architecture/API/security docs.
- Confirmed branch, file-write, and PR permissions. Work is isolated on `feat/project-foundation`; `main` has not been changed.
- GitHub Actions for the initial foundation passed on Python 3.11, 3.12, and 3.13.

## 2026-10-24 — Normalization, idempotent storage, analytics, and paper controls

- Added immutable normalized trade observations with deterministic SHA-256 IDs and strict validation for timestamp, side, asset, price, size, and wallet.
- Added SQLite trade storage, duplicate suppression, ingestion-run counters/status, invalid-row counts, and activity-only reports.
- Added a deterministic paper engine with quote freshness, market matching, depth, cash, inventory, price-bound, size, notional, position, fee, slippage, and kill-switch controls.
- Added `sync-trades`, `report`, and `paper-sim` CLI commands and deterministic tests.
- Account-level reconciliation remains explicitly unimplemented: wallet trades alone are not a complete position ledger. No live API smoke test, live orders, signing, or fund movement was performed.
- Validation is pending for the new commit until GitHub Actions completes. The existing foundation CI run passed all three Python versions.
