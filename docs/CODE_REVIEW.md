# Code review: `feat/project-foundation` (commit 44c58a9)

Scope: all of `src/`, `tests/`, CI, packaging and docs on that branch (about 1,100 lines).
Method: read every file, ran the baseline (13 tests passing), then reproduced each suspected problem
with a throwaway probe before changing anything. Only reproduced problems were fixed; everything
else is listed under "Not changed" with the reason.

**Verdict: needs changes, then good to merge.** The foundation is deliberately conservative: read-only
client, fail-closed envelope validation, Decimal arithmetic, kill switch, and honest "not P&L" labelling.
The problems below are robustness and correctness gaps around that core, not design errors.

## Findings and status

| # | Sev | Area | Problem (reproduced) | Status |
|---|-----|------|----------------------|--------|
| 1 | High | `numeric` / `models` / `store` | A row with `size = "1e9999999"` passes validation (finite, positive), is stored, and then every `report` raises `decimal.Overflow`. One bad upstream row permanently breaks reporting for that wallet. | Fixed: shared `parse_decimal` bounds magnitude (`< 1e15`); such rows are counted `invalid`. |
| 2 | High | `paper` | A quote with `ask = 0` (or a SELL at `bid = 0`) filled at price 0: free shares, or a sale for nothing. | Fixed: non-positive execution price is rejected. |
| 3 | High | `store` | `ResearchStore(":memory:")` was unusable. It opened a new connection per call and each in-memory database is private to its connection, so tables vanished (`no such table`). The code explicitly special-cased `:memory:`, so the intent was to support it. | Fixed: one connection for the store's lifetime; `close()` and context-manager support. |
| 4 | High | `cli` | `sync-trades` caught only `OSError/ValueError/RuntimeError/DataApiError`. A `sqlite3.Error` or Ctrl-C left the ingestion run `running` forever. | Fixed: any `Exception` marks the run `failed`; `BaseException` marks it `interrupted` and re-raises. |
| 5 | Med | `cli` | `--initial-cash abc` and `--max-order-size NaN` crashed with a raw `decimal.InvalidOperation` traceback (argparse only converts `ValueError`). | Fixed: `_decimal_arg` gives a proper usage error (exit 2); `RiskLimits` rejects non-finite values with `ValueError`. |
| 6 | Med | `data_api` | `Retry-After: nan` produced a `nan` sleep delay. HTTP 500/502/504 were not retried, only 429/503. No jitter. | Fixed: non-finite values fall back to backoff; 500/502/503/504/429 retry; 10% jitter; `sleep` is injectable for tests. |
| 7 | Med | `data_api` | A malformed `has_more`/`next_cursor` was detected only after that page's rows had been yielded, so callers saw partial data from a page that was then declared invalid. | Fixed: the whole page envelope is validated before any row is yielded. |
| 8 | Med | `store` | One connection plus one transaction per row (an fsync each) and an extra `SELECT` per row. Connections were never closed (`ResourceWarning` on 3.13). | Fixed: batched `record_rows` (500 rows per transaction), WAL, streaming `report`. `record_row` kept for compatibility. |
| 9 | Med | `cli` | `export-trades` and `paper-sim` wrote straight to the destination, so a mid-run failure left a truncated file that looks complete. | Fixed: write to a temp file beside the target, then atomic rename; cleaned up on failure. |
| 10 | Low | `cli` / `.env.example` | `POLYMARKET_DATA_API_BASE_URL` was documented but never read. | Fixed: honoured by the CLI; base URL must be `https` with no embedded credentials. |
| 11 | Low | `cli` | Wallet was not validated, so a typo burned a network round-trip (and, for `report`, silently returned zeros). | Fixed at the CLI boundary only (`0x` + 40 hex). The library client stays lenient. |
| 12 | Low | `cli` | `report` on a wrong `--db` path silently created an empty database and its parent directory. | Fixed: exits 1 with "database not found". |
| 13 | Low | `models` / `paper` | `int(float("inf"))` raised a bare `OverflowError` (reachable because `json.loads` accepts `Infinity`); `True` was accepted as timestamp `1`. | Fixed: shared `parse_int`. |
| 14 | Low | `cli` | Dead code: `_client_context` (never used, not even decorated) and a hand-rolled `_AsyncClientContext`. | Fixed: the client is an async context manager. |
| 15 | Low | tooling | README referenced `fixtures/paper-orders.jsonl`, which did not exist. CI had no lint/type check, default write token, and duplicate push+PR runs. `mypy --strict` reported 2 errors. | Fixed: fixture added; ruff + mypy in CI; `permissions: contents: read`; concurrency; pip cache; Dependabot; `py.typed`; warnings are errors in pytest. |

Tests went from 13 to 131. Each fix above has a regression test, including the ones where my first
attempt was wrong (see the `copy_abs` note below).

## Not changed (and why)

- **`event_id` is a hash of the entire raw row.** If the API ever adds or changes a field on an existing
  fill, the same trade is stored twice and volume is double counted. A natural key (for example a
  transaction hash plus asset and side) would be safer, but I did not call the live API, so I cannot
  confirm which fields are stable. Verify against the official v2 reference before changing identity;
  doing so needs a migration (schema version is now tracked in `PRAGMA user_version`).
- **Sync is not incremental.** Every run re-pulls the full window. A `--resume` flag that starts from
  the newest stored timestamp is the natural next step. Runs killed with SIGKILL can still be left
  `running`; a startup sweep would close those.
- **Fee model.** `fee_rate * notional` is a flat-rate placeholder. If Polymarket's real fee depends on
  price or market type, results will be biased. It is a user-supplied assumption and documented as one.
- **Paper engine realism.** All-or-none top-of-book fills, no partial fills, no tick-size rounding,
  no persisted portfolio. The order-intent age check reuses `max_quote_age_seconds`; a separate limit
  would be cleaner. None of these are bugs given the documented assumptions.
- **`Retry-After` as an HTTP-date** is not parsed; it falls back to exponential backoff.
- **Response size** is bounded only by `limit <= 1000` rows per page.
- **GitHub Actions are pinned to tags, not commit SHAs.** Pinning needs SHAs verified against the
  upstream repos, which I could not do offline. Dependabot will now keep them current.
- **Live trading** remains out of scope. Everything in `SECURITY.md` still applies before that changes.

## Design notes: what to revisit as this grows

```
Data API --HTTPS--> data_api.PolymarketDataClient --raw rows--> models.TradeEvent --> store.ResearchStore (SQLite/WAL)
                                                                                         |
quote snapshots (JSONL) --> paper.QuoteSnapshot --> paper.PaperEngine (in-memory)        +--> report / events
```

- The three boxes are already separated by plain data (dicts, frozen dataclasses), which is the right
  seam. Keep strategy code behind an interface that emits order intents, so the same intents can drive
  the paper engine now and an execution adapter later.
- SQLite with one writer is the right size for single-wallet research. Move to Postgres only if you
  need concurrent writers or many wallets.
- If ingestion fans out to many wallets, add a shared rate limiter in front of the client rather than
  relying on per-request retries.
- Before any live execution: authoritative position and balance reconciliation, persistent portfolio
  state, idempotent order IDs, and out-of-sample evidence of an edge net of fees and slippage.

## Notes on the fixes

- The magnitude check uses `Decimal.copy_abs()`, not `abs()`. `abs()` applies the arithmetic context and
  raises `Overflow` itself on `1e9999999`, which is exactly the input being screened. My first version
  used `abs()` and the new regression test caught it.
- A price like `1e-9999999999` is legal (it is in `[0, 1]`) and does not break reports, so it is still
  accepted. It prints an unusual exponent in `observed_gross_notional_usdc` and nothing worse.
