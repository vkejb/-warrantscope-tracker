# EARLY_SIGNAL_OBSERVATION_V1

## Boundary

This is append-only telemetry inside `RealtimeExpandedShadow`. It has no broker,
account, order adapter, live switch, notification callback, or order method.
It does not alter the frozen official candidate, V2 volume gate, three-minute
continuation, manual-review notification, or account selection path.

## Causal states

The full expanded universe receives a cheap 30-second prefilter using only data
already received: fresh price, TWD190 cap, above-VWAP, existing opening/VWAP
anti-chase limits, and the existing 0050 regime-relative-strength requirement.
At most the strongest 40 names receive the full feature calculation per
interval. This cap controls observer load and is not a trading rank.

The five exact production-direction checks are:

1. 60-second active-volume delta;
2. 60-second large-trade delta;
3. price above causal session VWAP;
4. current five-level book imbalance;
5. price above the causal breakout boundary.

State transitions are monotonic for the session:

- `PREWATCH`: at least three of five exact checks pass;
- `ARMED`: at least four of five pass;
- `EARLY_ENTRY_READY_OBSERVED`: all five and the unchanged frozen V2 recent
  volume-acceleration gate pass.

After `ARMED`, accepted ticks can trigger a throttled five-second re-evaluation.
Observation stops after the existing last-entry time. The early-ready state is
not an approved signal: it does not include the official confirmation streak,
buy-concentration gate, final account ranking, or three-minute continuation.

## Evidence

Transitions are appended as `EARLY_STATE_TRANSITION`. Official candidate and
selected-signal events include the prior early timeline and measurable lead
time when available. The terminal summary contains state counts and all nonempty
timelines. `notification_enabled=false`, `order_enabled=false`,
`actual_orders=0`, and `actual_fills=0` are explicit invariants.
