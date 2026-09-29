# Signal quality diagnostic

This is a retrospective, read-only diagnostic. It does not change entry, exit, sizing, or live behavior.

- Trades: 18 (17 scorable)
- Winners / losers: 6 / 11
- Independent signals may overlap and are not one executable portfolio.

## Winner versus loser

| Outcome | Trades | Avg score | Avg volume strength | Avg large-trade strength | Avg net PnL | Avg held MFE |
|---|---:|---:|---:|---:|---:|---:|
| WINNER | 6 | 0.619 | 0.639 | 0.919 | 4,050 | 6,199 |
| LOSER | 11 | 0.745 | 0.801 | 0.929 | -2,371 | 459 |

## Score buckets

| Score bucket | Trades | Winners | Win rate | Avg net PnL | Total net PnL |
|---|---:|---:|---:|---:|---:|
| LT_0_55 | 3 | 2 | 66.7% | 3,586 | 10,759 |
| 0_55_TO_0_65 | 1 | 0 | 0.0% | -3,591 | -3,591 |
| 0_65_TO_0_75 | 7 | 4 | 57.1% | 589 | 4,121 |
| GE_0_75 | 6 | 0 | 0.0% | -2,178 | -13,067 |

## Session split

| Session | Trades | Winners | Winner avg score | Loser avg score | Total net PnL |
|---|---:|---:|---:|---:|---:|
| 20260922 | 1 | 1 | 0.663 | N/A | 4,442 |
| 20260923 | 10 | 3 | 0.573 | 0.768 | -2,570 |
| 20260924 | 6 | 2 | 0.664 | 0.704 | -3,650 |

## Correlation diagnostic

| Feature | Spearman vs net PnL | Spearman vs held MFE | Spearman vs win | LOO PnL range | Stable positive? |
|---|---:|---:|---:|---:|---:|
| score | -0.512 | -0.591 | -0.578 | -0.644 to -0.441 | False |
| volume_strength | -0.381 | -0.462 | -0.453 | -0.502 to -0.293 | False |
| large_trade_strength | -0.227 | -0.301 | -0.147 | -0.408 to -0.150 | False |

## Conclusion

Losers have the higher average composite score in this sample. The score measures the intensity of recent flow and breakout conditions, but it is not calibrated as a probability of profit. Strong readings can also occur during late-stage acceleration.

All three archived sessions are labelled PARTIAL_SESSION by their source manifests; 2026-09-23 also records callback errors. These limitations make the results useful for diagnosis, not production validation.

Do not add a minimum-score production filter from this sample. Continue collecting full-session data and test whether score interacts with extension, time of day, post-entry flow decay, and market regime.
