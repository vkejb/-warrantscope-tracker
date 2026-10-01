# Non-time path-failure stop validation

No minimum holding time is used. Existing entries, -1R disaster stop, MFE_V1, costs and slippage remain unchanged.

## Cohorts

- Base signals: 10
- Additional unique near-misses: 18
- Expanded unique signals: 28

## Expanded cohort: top robust variants

A robust row improves every leave-one-trade-out sample and harms no baseline winner.

| Rank | Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Stops |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | PATH_LOSS_30PCT_R_MFE_20PCT_R_FLOW_2OF3 | -31726 | +18659 | 3/25 | 0.401148 | -2119 | 40255 | 18 |
| 2 | PATH_LOSS_20PCT_R_MFE_20PCT_R_TWO_OF3 | -31926 | +18459 | 3/25 | 0.399639 | -2127 | 40455 | 18 |
| 3 | PATH_LOSS_30PCT_R_MFE_20PCT_R_TWO_OF3 | -32125 | +18260 | 3/25 | 0.398149 | -2135 | 40654 | 18 |
| 4 | PATH_LOSS_40PCT_R_MFE_20PCT_R_FLOW_2OF3 | -33220 | +17165 | 3/25 | 0.390145 | -2179 | 41549 | 18 |
| 5 | PATH_LOSS_40PCT_R_MFE_20PCT_R_TWO_OF3 | -33420 | +16965 | 3/25 | 0.388718 | -2187 | 41749 | 18 |
| 6 | PATH_LOSS_30PCT_R_MFE_10PCT_R_FLOW_2OF3 | -34120 | +16265 | 3/25 | 0.383804 | -2215 | 42649 | 17 |
| 7 | PATH_LOSS_20PCT_R_MFE_10PCT_R_TWO_OF3 | -34320 | +16065 | 3/25 | 0.382423 | -2223 | 42849 | 17 |
| 8 | PATH_LOSS_30PCT_R_MFE_10PCT_R_TWO_OF3 | -34519 | +15866 | 3/25 | 0.381058 | -2231 | 43048 | 17 |
| 9 | PATH_LOSS_40PCT_R_MFE_10PCT_R_FLOW_2OF3 | -35215 | +15170 | 3/25 | 0.376361 | -2259 | 43544 | 17 |
| 10 | PATH_LOSS_40PCT_R_MFE_10PCT_R_TWO_OF3 | -35415 | +14970 | 3/25 | 0.375033 | -2267 | 43744 | 17 |
| 11 | PATH_LOSS_50PCT_R_MFE_20PCT_R_FLOW_2OF3 | -35714 | +14671 | 3/25 | 0.373065 | -2279 | 43444 | 18 |
| 12 | PATH_LOSS_50PCT_R_MFE_20PCT_R_TWO_OF3 | -35714 | +14671 | 3/25 | 0.373065 | -2279 | 43444 | 18 |
| 13 | PATH_LOSS_50PCT_R_MFE_10PCT_R_FLOW_2OF3 | -37510 | +12875 | 3/25 | 0.361662 | -2350 | 45240 | 17 |
| 14 | PATH_LOSS_50PCT_R_MFE_10PCT_R_TWO_OF3 | -37510 | +12875 | 3/25 | 0.361662 | -2350 | 45240 | 17 |
| 15 | PATH_LOSS_20PCT_R_MFE_20PCT_R_VWAP_RS | -38809 | +11576 | 3/25 | 0.35384 | -2402 | 42947 | 9 |

## Best result by cohort

### Base signals

- Baseline: -9627 TWD, PF 0.543268, max DD 17394.
- Existing 120-second reference: -4437 TWD, PF 0.720733, max DD 14399.
- Best no-winner-harm variant: PATH_LOSS_20PCT_R_MFE_10PCT_R_FLOW_2OF3.
- Result: -8227 TWD (+1400), PF 0.581919, max DD 18389.

### Additional near-misses

- Baseline: -40758 TWD, PF 0.193853, max DD 40758.
- Existing 120-second reference: -25543 TWD, PF 0.277303, max DD 25543.
- Best no-winner-harm variant: PATH_LOSS_30PCT_R_MFE_10PCT_R_ANY_1OF3.
- Result: -17314 TWD (+23444), PF 0.36146, max DD 17314.

### Expanded unique signals

- Baseline: -50385 TWD, PF 0.296662, max DD 54523.
- Existing 120-second reference: -29980 TWD, PF 0.414819, max DD 37710.
- Best no-winner-harm variant: PATH_LOSS_30PCT_R_MFE_20PCT_R_FLOW_2OF3.
- Result: -31726 TWD (+18659), PF 0.401148, max DD 40255.

## False-exit review for the best non-time variant

The best variant worsened 3 trades that later recovered under the baseline path:

- 20260923 2221: baseline -521 TWD versus path stop -3014 TWD (-2493).
- 20260924 3605: baseline -87 TWD versus path stop -2580 TWD (-2493).
- 20260930 1709: baseline -811 TWD versus path stop -2806 TWD (-1995).

## Interpretation

- Prefer a configuration only if it preserves baseline winners, improves multiple losses, and survives leave-one-trade-out deletion.
- The 120-second row is a comparison reference only; it is not part of the non-time parameter grid.
- A less-negative result is loss reduction, not proof of positive expectancy.
- No configuration is enabled in production or live trading.

## Limitations

- The grid is a diagnostic comparison, not fitted production parameters.
- Older 20260922-20260924 signals have no synchronized 0050; VWAP_RS cannot trigger for them.
- Additional near-miss entries are counterfactual and overlap in clock time.
- The 20260930 session is stitched and has four untimestamped callback errors.
- Twenty-eight unique signals remain too small for production selection.
