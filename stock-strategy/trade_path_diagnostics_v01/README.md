# Trade path diagnostics v0.1

Read-only MAE/MFE, holding-time, and 5/10/15/30-minute winner/loser diagnostics.
It reuses the independent historical signal cohort and the current exit policy
only to label final outcomes. It does not modify or import itself into production
strategy or live execution code.

Run with one or more historical Yuanta session directories:

```bash
PYTHONPATH=stock-strategy python3 -m trade_path_diagnostics_v01.main \
  --session 20260922=/path/to/run1,/path/to/run2 \
  --session 20260923=/path/to/run3 \
  --output-dir stock-strategy/trade_path_diagnostics_v01/results
```

Incomplete horizons remain null. A horizon is complete only when recorded data
reaches the target and the latest exit quote is no more than the strategy's
existing maximum tick-staleness allowance (5 seconds) old.

The independent `early_failure_main` command runs the fixed 75-candidate
5/10/15-minute early-failure grid. It requires both a negative current-PnL R
threshold and a low MFE-progress R threshold, preserves earlier original exits,
and writes per-trade plus leave-one-trade-out diagnostics. It is not imported by
live code.
