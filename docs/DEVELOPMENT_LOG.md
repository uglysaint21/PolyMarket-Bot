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

## 2026-10-24 — Review hardening (`enhance/review-hardening`)

- Reviewed `feat/project-foundation` at 44c58a9; findings, severities and deferred items are in `docs/CODE_REVIEW.md`.
- Fixed reproduced defects: unbounded-decimal report poisoning, zero-price paper fills, unusable `:memory:` store, runs stuck `running` after DB errors or Ctrl-C, raw `decimal` tracebacks from CLI flags, `Retry-After: nan`, missing retry on 500/502/504, partial rows leaked from invalid pages, truncated output files, unused `POLYMARKET_DATA_API_BASE_URL`.
- Added batching/WAL/schema version to the store, wallet and https validation, `fixtures/paper-orders.jsonl`, ruff + mypy in CI, least-privilege workflow permissions, Dependabot.
- Validation: 131 tests pass locally with warnings treated as errors; `ruff check` and `mypy --strict` are clean. GitHub Actions has not yet run on this branch. No live API call was made.
- Known limitations unchanged: row-hash identity, non-incremental sync, flat fee placeholder (see the review document).
