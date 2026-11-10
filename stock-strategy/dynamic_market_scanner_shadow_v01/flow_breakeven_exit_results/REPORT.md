# Flow-reversal cost-aware breakeven exit study

Known-archive paired diagnostic only; no live behavior changed.

| Scenario | Variant | Scored | W/L/BE | Win rate | Net PnL | Delta vs baseline | PF | Max DD | Flow exits | Loss avoided | Profit surrendered | Net ex best trade |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | BASELINE | 9 | 3/6/0 | 33.3% | -5732 | 0 | 0.62 | 8841 | 0 | 0 | 0 | -9783.0 |
| BASE | FLOW_BE_ANY_POSITIVE | 9 | 3/6/0 | 33.3% | -5732 | 0 | 0.62 | 8841 | 0 | 0 | 0 | -9783.0 |
| BASE | FLOW_BE_0_25R | 9 | 3/6/0 | 33.3% | -5732 | 0 | 0.62 | 8841 | 0 | 0 | 0 | -9783.0 |
| STRESS | BASELINE | 6 | 3/3/0 | 50.0% | 9538 | 0 | 1.94 | 6223 | 0 | 0 | 0 | -2470.0 |
| STRESS | FLOW_BE_ANY_POSITIVE | 6 | 3/3/0 | 50.0% | 9538 | 0 | 1.94 | 6223 | 0 | 0 | 0 | -2470.0 |
| STRESS | FLOW_BE_0_25R | 6 | 3/3/0 | 50.0% | 9538 | 0 | 1.94 | 6223 | 0 | 0 | 0 | -2470.0 |

## Boundaries

- Thresholds were fixed before this run and were not optimized.
- Historical archives are already known and are not out-of-sample.
- Missing flow never counts as reversal; unfilled entries remain explicit.
- A positive result would require later untouched full-session shadow validation.

## Attribution

- BASE had 2 losing trades with positive executable MFE; neither completed the two-window flow reversal.
- The rule activated on 1 BASE and 1 STRESS winner, but neither retouched cost-aware breakeven before the existing exit.
- A post-hoc immediate exit at those flow confirmations would change matched P&L by -2993 TWD in BASE and -9079 TWD in STRESS.

## 2026-11-10 freeze-day case (excluded)

- Baseline net P&L: -3544 TWD.
- ANY_POSITIVE result: -1299 TWD; delta 2245 TWD. Flow confirmation arrived after executable P&L was already negative, so this is loss reduction, not a breakeven fill.
- 0.25R result: -3544 TWD; reason MFE_THRESHOLD_NOT_REACHED.
- This observed case is not included in the 16-session metrics and cannot validate the rule.
