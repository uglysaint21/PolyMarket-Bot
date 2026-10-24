# Polymarket API reference notes

## Primary sources

- **Official Data API v2 reference:** https://data-api.polymarket.com/v2/docs
- **Polymarket developer documentation:** https://docs.polymarket.com/

The read-only client uses `GET https://data-api.polymarket.com/v2/trades` with `user`, `start`, `end`, `limit`, and `taker_only=false`; subsequent pages use the opaque `cursor` returned by the prior response. The expected paged envelope is `{ "data": [...], "pagination": { "has_more": ..., "next_cursor": ... } }`.

## Contract and safety notes

- The official v2 reference describes cursor-only pagination for feeds and says the same filters must be repeated on each page; do not use `offset` for trades.
- Window bounds for wallet trades are Unix epoch seconds. A `start=1` request has special full-history semantics in the current docs; this scaffold requires an explicit positive bounded start/end to avoid accidental unbounded pulls.
- Treat missing/inconsistent pagination metadata as an incomplete ingestion, not an empty result.
- The public Data API is not an execution API. This client has no authenticated order placement.
- The local store fingerprints the entire normalized source row for idempotency. Source changes to a row produce a distinct fingerprint; this is not a protocol-level unique fill ID.
- Activity metrics are descriptive only. Never infer realized P&L or complete positions from this table alone.
- Verify the live official reference before extending the client; the API and rate-limit behavior may change.

The project PDF `Polymarket API - Complete Developer Reference Guide.pdf` is supplementary background and may contain older endpoint details.
