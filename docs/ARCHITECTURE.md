# Architecture and implementation sequence

## Current vertical slices

1. **Public data ingestion:** bounded requests to the Polymarket Data API; validate response envelopes and cursor metadata; record run status and preserve raw source rows.
2. **Normalization and storage:** immutable normalized observations, deterministic source-row fingerprints, SQLite persistence, idempotent insert, invalid-row counts, and ingestion history.
3. **Research and analytics:** reports describe observed rows, share volume, and gross notional only. These are not positions, cash flows, realized P&L, or evidence of an edge.
4. **Paper execution:** consumes supplied quote snapshots and hypothetical order intents; applies configurable fee/slippage assumptions, displayed depth, inventory/cash checks, stale-data guard, max order/position/notional limits, and kill switch.
5. **Account reconciliation:** not implemented. True reconciliation needs authoritative current positions, balances, fills, settlements, and a defined source-of-truth; public wallet trades alone are insufficient.
6. **Live execution:** out of scope pending explicit authorization, API/SDK verification, paper acceptance criteria, operational monitoring, reconciliation, and a security review.

## Paper simulator assumptions

- All-or-none fills at the top ask for BUY and top bid for SELL, adjusted by explicit slippage basis points.
- The supplied quote's displayed size is treated as executable depth for the whole simulated order.
- Fee rate defaults to zero unless supplied by the operator; this is a simulation input, not a claim about current Polymarket fees.
- The simulator tracks only its own in-memory paper cash and token quantities. It does not persist state or infer inventory from observed public trades.
- Orders are rejected for stale/future quotes or intents, insufficient displayed depth, insufficient cash/inventory, binary-token price bounds, configured limits, or kill-switch activation.

## Engineering boundaries

- External I/O is behind clients that can be replaced by deterministic mocks.
- Keep research ingestion, simulation, and any future live execution separated by explicit interfaces.
- Use Rust only when concurrency or runtime profiling justifies it; use C++ only with measured performance need. Add Solidity only if a verified integration requirement exists.
- Any future strategy must state its hypothesis, inputs, entry/exit rules, costs, liquidity assumptions, risk limits, failure modes, and out-of-sample validation plan.
