# Yuanta market context v0.1

Read-only normalization and fail-closed shadow gates for official Yuanta data
that the current direction engine does not yet consume. This package has no
login, broker connection or order-submission code and is not imported by the
live runtime.

Priority inputs:

1. `GetStockInformation`: day-trade code, sell-first suspension, short inventory,
   below-reference short permission and warning status.
2. `GetWatchListAll` / `SubscribeWatchlistAll`: previous close, open reference,
   limits, cumulative inside/outside volume, turnover and aggregate order counts.
3. Existing `SubscribeFiveTickA`: retain individual levels and derive L1/weighted
   imbalance, microprice and depth depletion instead of keeping only summed depth.

`PreTradeEvidenceGate` defaults to fail closed when eligibility or quote context
is missing. It supports LONG and SHORT validation, but this module does not enable
short selling or change production behavior.

See `SDK_DATA_AUDIT.md` for the source-to-feature inventory and rollout order.
