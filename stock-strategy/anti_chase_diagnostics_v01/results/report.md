# Anti-chase diagnostics

Backtest-only entry-filter diagnostics using information available at the signal time. No strategy or live behavior was changed.

Baseline scorable trades: 17; baseline net PnL: -1,778 TWD.

| Gate | Kept | Avoided losers | Removed winners | Filtered net | Delta | Win rate | PF | LOO min | Shadow candidate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ANTI_CHASE_BALANCED_V1 | 8 | 9 | 0 | 20,029 | 21,807 | 75.0% | 5.69 | 17,862 | True |
| OPEN_MAX_2PCT | 10 | 7 | 0 | 15,702 | 17,480 | 60.0% | 2.83 | 13,535 | True |
| OPEN_MAX_3PCT | 11 | 6 | 0 | 14,146 | 15,924 | 54.5% | 2.39 | 11,979 | True |
| ANTI_CHASE_LOOSE_V1 | 11 | 6 | 0 | 14,146 | 15,924 | 54.5% | 2.39 | 11,979 | True |
| VWAP_MAX_125BP | 11 | 6 | 0 | 13,553 | 15,331 | 54.5% | 2.26 | 11,386 | True |
| OPEN_MAX_5PCT | 12 | 5 | 0 | 12,819 | 14,597 | 50.0% | 2.12 | 10,652 | True |
| VWAP_MAX_150BP | 13 | 4 | 0 | 9,226 | 11,004 | 46.2% | 1.61 | 7,059 | True |
| VWAP_MAX_200BP | 14 | 3 | 0 | 5,281 | 7,059 | 42.9% | 1.28 | 3,468 | True |
| VOLUME_STRENGTH_MAX_0_95 | 14 | 3 | 0 | 3,891 | 5,669 | 42.9% | 1.19 | 2,120 | True |
| RETURN_1M_MAX_150BP | 17 | 0 | 0 | -1,778 | 0 | 35.3% | 0.93 | 0 | False |
| RETURN_1M_MAX_200BP | 17 | 0 | 0 | -1,778 | 0 | 35.3% | 0.93 | 0 | False |
| BREAKOUT_MAX_75BP | 17 | 0 | 0 | -1,778 | 0 | 35.3% | 0.93 | 0 | False |
| BREAKOUT_MAX_100BP | 17 | 0 | 0 | -1,778 | 0 | 35.3% | 0.93 | 0 | False |
| ANTI_CHASE_STRICT_V1 | 7 | 9 | 1 | 15,871 | 17,649 | 71.4% | 4.72 | 13,704 | False |
| VWAP_MAX_100BP | 10 | 6 | 1 | 9,395 | 11,173 | 50.0% | 1.87 | 7,228 | False |
| VOLUME_STRENGTH_MAX_0_8 | 10 | 6 | 1 | 6,987 | 8,765 | 50.0% | 1.45 | 5,216 | False |
| EXTENSION_BALANCED_V1 | 12 | 4 | 1 | 4,784 | 6,562 | 41.7% | 1.32 | 2,617 | False |
| VOLUME_STRENGTH_MAX_0_9 | 12 | 4 | 1 | 3,305 | 5,083 | 41.7% | 1.17 | 1,534 | False |
| EXTENSION_LOOSE_V1 | 13 | 3 | 1 | 839 | 2,617 | 38.5% | 1.04 | -974 | False |
| BREAKOUT_MAX_50BP | 15 | 1 | 1 | -6,103 | -4,325 | 33.3% | 0.73 | -7,874 | False |
| RETURN_1M_MAX_100BP | 15 | 1 | 1 | -8,310 | -6,532 | 33.3% | 0.66 | -7,874 | False |
| RETURN_5M_MAX_3PCT | 16 | 0 | 1 | -6,220 | -4,442 | 31.2% | 0.76 | -4,442 | False |
| RETURN_5M_MAX_4PCT | 16 | 0 | 1 | -6,220 | -4,442 | 31.2% | 0.76 | -4,442 | False |
| RETURN_5M_MAX_5PCT | 16 | 0 | 1 | -6,220 | -4,442 | 31.2% | 0.76 | -4,442 | False |
| VWAP_MAX_75BP | 7 | 8 | 2 | 11,874 | 13,652 | 57.1% | 3.04 | 9,707 | False |
| RETURN_1M_MAX_75BP | 12 | 2 | 3 | -14,784 | -13,006 | 25.0% | 0.35 | -15,132 | False |
| RETURN_5M_MAX_2PCT | 14 | 0 | 3 | -12,819 | -11,041 | 21.4% | 0.51 | -11,041 | False |
| EXTENSION_STRICT_V1 | 7 | 6 | 4 | -5,362 | -3,584 | 28.6% | 0.50 | -7,529 | False |
| BREAKOUT_MAX_25BP | 4 | 8 | 5 | -4,033 | -2,255 | 25.0% | 0.38 | -6,200 | False |
| SCORE_LT_0_70_POSTHOC_REFERENCE | 8 | 9 | 0 | 20,122 | 21,900 | 75.0% | 5.82 | 17,955 | False |

## Interpretation

The strongest shadow-only candidate under the predeclared robustness ordering is ANTI_CHASE_BALANCED_V1: it avoided 9 losers, removed no winners, and improved diagnostic net PnL by 21,807 TWD. This is not production validation.

The score<0.70 result is included only as a post-hoc reference and is ineligible for recommendation.
All source sessions are partial, and one session has callback errors. Collect full sessions before any production change.
