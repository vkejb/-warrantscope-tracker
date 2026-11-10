# Broker UAT boundary

This UAT layer is read-only and mock-only. It is not PROD approval.

## Allowed

- Inspect the installed LaunchAgent's exact code/runtime binding.
- Open the local order ledger with SQLite `mode=ro`.
- Query only `GetRealReport`, `GetRealReportMerge`, `GetStoreSummary`, and
  `GetOrderTradeReport` when the separate read-only probe is explicitly run.
- Exercise submit/ack/partial-fill/cancel/reconcile/force-flat transitions with
  mocks that cannot reach the broker.

## Forbidden

- Sending, changing, cancelling, or replacing any real order.
- Enabling LIVE/PROD gates or changing the installed trading LaunchAgent.
- Treating mock success as proof of broker connectivity or forced-flat safety.
- Writing account identifiers, credentials, certificates, tokens, or raw
  broker payloads to the repository.

Broker-backed partial-fill, cancel/replace, and forced-flat evidence remain
`UNKNOWN` until a separately authorised controlled exercise exists. A read-only
inventory query can reduce reconciliation uncertainty but cannot clear those
three execution risks.
