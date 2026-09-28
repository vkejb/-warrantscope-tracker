# Stop-loss diagnostic

Same signals, entries, sizes, fees, tax, slippage and non-stop exits.

| Variant | Net | PF | Avg loss | Max loss | Max DD | Stops | Recovered | Before 10:30 net | Before 10:30 PF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASELINE_5000 | -6667.00 | 0.73 | -3559.29 | -5441.00 | 12865.00 | 3 | 0 | 1461.00 | 1.10 |
| HARD_4500 | -4969.00 | 0.79 | -3316.71 | -4942.00 | 11659.00 | 3 | 0 | 2660.00 | 1.20 |
| HARD_4000 | -3671.00 | 0.83 | -3131.29 | -4443.00 | 10761.00 | 4 | 0 | 3459.00 | 1.28 |
| HARD_3750 | -2174.00 | 0.89 | -2917.43 | -3945.00 | 9864.00 | 4 | 0 | 4458.00 | 1.39 |
| HARD_3500 | -1574.00 | 0.92 | -2831.71 | -3945.00 | 9664.00 | 4 | 0 | 5058.00 | 1.47 |
| HARD_3250 | -4030.00 | 0.82 | -2733.62 | -3446.00 | 10358.00 | 5 | 1 | 5856.00 | 1.58 |
| HARD_3000 | -10663.00 | 0.61 | -2711.30 | -3446.00 | 17191.00 | 7 | 3 | -1027.00 | 0.93 |
| HARD_2500 | -11769.00 | 0.57 | -2478.55 | -3094.00 | 17012.00 | 9 | 4 | -2234.00 | 0.86 |
| TIME_3M_NO_POSITIVE_MFE | -8174.00 | 0.60 | -1590.69 | -2995.00 | 13417.00 | 13 | 6 | 167.00 | 1.01 |
| TIME_5M_NO_POSITIVE_MFE | -12767.00 | 0.49 | -1944.00 | -3394.00 | 18010.00 | 13 | 6 | -2330.00 | 0.84 |
| TIME_10M_COST_ZONE | -9079.00 | 0.60 | -1888.42 | -3992.00 | 15002.00 | 11 | 5 | 1160.00 | 1.09 |
| HYBRID_SOFT_2000_NO_POS_MFE_HARD_4000 | -14773.00 | 0.46 | -2105.00 | -2448.00 | 20016.00 | 12 | 6 | -2332.00 | 0.84 |
| HYBRID_SOFT_2500_NO_POS_MFE_HARD_4000 | -11769.00 | 0.57 | -2478.55 | -3094.00 | 17012.00 | 9 | 4 | -2234.00 | 0.86 |

## Diagnostic readout

- Best all-trade result: `HARD_3500` at -1574.00 TWD; it remains negative.
- Best before-10:30 result: `HARD_3250` at 5856.00 TWD, PF 1.58, and 613.00 TWD after removing its largest winner.
- Tightening below this region is not monotonic: HARD_3000 and tighter variants cut trades that later recovered.

In-sample diagnostic from three quality-limited sessions.
No variant is enabled in production or eligible for live promotion.
