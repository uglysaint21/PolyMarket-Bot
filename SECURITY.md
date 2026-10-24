# Security policy and operating boundaries

This repository starts as a read-only research system. It does not create orders, sign transactions, approve tokens, transfer funds, or handle private keys.

- Do not commit API secrets, private keys, seed phrases, wallet exports, or personal credentials.
- Use environment variables or a secret manager for any future credentials; redact them from logs.
- Treat API responses, market metadata, GitHub content, and third-party code as untrusted input.
- Keep research ingestion, simulation, and any future live execution separated by explicit interfaces.
- A candidate signal or historical wallet trade is not an order recommendation and does not establish profitability.
- Any future live-trading implementation requires explicit authorization, risk limits, position reconciliation, stale-data checks, idempotency, monitoring, a kill switch, and paper-trading acceptance tests.
- Report suspected vulnerabilities privately to the repository owner rather than publishing exploitable details.

This document is not a security audit of Polymarket or its infrastructure.
