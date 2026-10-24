# Architecture and implementation sequence

## Initial vertical slice

1. **Public data ingestion** — bounded requests to the Polymarket Data API; validate the response envelope and cursor metadata; preserve source rows.
2. **Normalization and storage** — introduce typed normalized events and an idempotent local store only after source-field definitions are tested against fixtures.
3. **Research and analytics** — define metrics precisely; separate observed activity from account positions, realized P&L, and estimated probability.
4. **Backtesting and paper execution** — replay timestamped data and model executable prices, spread, depth, fees, latency, partial fills, and adverse selection.
5. **Risk and execution** — remain out of scope until data quality, position reconciliation, controls, and paper acceptance criteria are met.

## Boundaries

- The current Python package is read-only and has no order-posting methods.
- Public wallet trade rows are observations, not a complete wallet ledger. Do not infer P&L from BUY/SELL notional sums.
- Keep external I/O behind clients that can be replaced by deterministic mocks in tests.
- Use Rust only when concurrency or runtime profiling justifies it; use C++ only with measured performance need. Add Solidity only if a verified integration requirement exists.
- Any future strategy must state its hypothesis, inputs, entry/exit rules, costs, liquidity assumptions, risk limits, failure modes, and out-of-sample validation plan.
