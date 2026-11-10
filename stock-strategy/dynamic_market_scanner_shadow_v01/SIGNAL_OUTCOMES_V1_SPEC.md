# EXPANDED_APPROVED_SIGNAL_OUTCOMES_V1

This diagnostic freezes 1, 3, 5, 15, and 30 minute checkpoints before reading
their results. It evaluates every first-per-symbol production-engine-approved
signal in the parent 16-session expanded-universe replay.

Two tracks are kept separate:

- `original`: production proxy entry at the original signal.
- `delayed`: entry at the causal 60-second checkpoint, when quote, spread, and
  whole-lot data made that delayed entry evaluable.

Each checkpoint marks a long exit at the latest best bid already received by
that exact time and rejects a stale book. Net P&L includes the existing modeled
round-trip commission and sell tax. Thirty-minute MFE/MAE use observed best-bid
updates only.

The sum across all signals is diagnostic, not account performance: signals can
overlap, share a date, and exceed the one-trade/shared-capital constraint. No
threshold is fitted and no best horizon becomes a strategy from this table.
