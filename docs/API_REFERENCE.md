# Polymarket API reference notes

## Primary source

- **Official Data API v2 reference:** https://data-api.polymarket.com/v2/docs
- **Polymarket developer documentation:** https://docs.polymarket.com/

The code in this initial slice reads the public `GET /v2/trades` feed at `https://data-api.polymarket.com/v2/trades`. It supplies the wallet, start/end epoch-second window, page limit, and `taker_only=false`; subsequent pages use the opaque `cursor` value returned by the previous response. The expected envelope is `{ "data": [...], "pagination": { "has_more": ..., "next_cursor": ... } }`.

## Integration rules

- Re-check the official reference before changing endpoint parameters or relying on response fields; API behavior may change.
- Preserve the same cohort filters across pages and never substitute numeric `offset` pagination for the cursor contract.
- Treat missing or inconsistent pagination metadata as an incomplete ingestion, not an empty result.
- Respect HTTP errors and rate limiting. Retry only bounded transient failures and avoid unbounded request loops.
- The public Data API is not an execution API. This client contains no authenticated order placement.
- Timestamp windows in this client are Unix epoch seconds and are validated before making a request.

The API contract above is an implementation assumption documented for this scaffold; verify it against the official reference when extending or deploying the client. The project PDF `Polymarket API - Complete Developer Reference Guide.pdf` is supplementary background and may describe endpoints that have since changed.
