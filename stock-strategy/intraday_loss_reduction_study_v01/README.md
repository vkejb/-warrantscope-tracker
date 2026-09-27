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
