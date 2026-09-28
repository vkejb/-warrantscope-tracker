# Intraday loss-reduction study v0.1

Fixed, non-optimized admission-gate diagnostic over the existing 18-trade
independent first-signal cohort. It does not alter stock selection, signals,
entries, exits, sizing, the broker adapter or live safety gates.

Run from `stock-strategy`:

```bash
python3 -m intraday_loss_reduction_study_v01.main
```

The input cohort overlaps and is based on three incomplete/quality-limited
sessions. Aggregate PnL is diagnostic, not an executable NT$190,000 portfolio.
Theoretical SHORT rows also lack contemporaneous broker eligibility and inventory
evidence. Every output therefore keeps `eligible_for_live_promotion=false`.

The controlled stop-loss replay uses the original tick paths:

```bash
python3 -m intraday_loss_reduction_study_v01.stop_loss_main \
  --session 20260922=/path/to/run1,/path/to/run2 \
  --session 20260923=/path/to/run \
  --session 20260924=/path/to/run
```

It compares fixed TWD hard stops, no-positive-MFE time stops and soft/hard
hybrids while preserving all other exits. Stops fill at the first observed
executable quote after the threshold, so gaps never receive perfect fills.

The indicator-stop replay treats the fixed TWD amount only as a disaster cap.
Its primary exit evidence is the causal Yuanta trade-flow direction, large-trade
flow and aggregated five-level book imbalance, optionally confirmed by a VWAP
break or book depletion:

```bash
python3 -m intraday_loss_reduction_study_v01.indicator_stop_main \
  --session 20260922=/path/to/run1,/path/to/run2 \
  --session 20260923=/path/to/run \
  --session 20260924=/path/to/run
```

Only fields already present in the historical archives are used. Newly audited
`GetStockInformation` and `GetWatchListAll` fields cannot be retroactively
claimed when those callbacks were not saved in the original sessions.
