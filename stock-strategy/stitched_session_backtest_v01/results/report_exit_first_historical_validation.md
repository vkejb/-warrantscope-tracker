# Exit-first historical validation

All variants use the same ten matched long trades and the existing cost, tax, slippage and MFE exit model. Only the backtest-only no-progress checkpoint changes.

## Coverage

- Historical matched long signals (2026-09-22 to 2026-09-24): 7
- Recent fixed one-trade-per-day sessions (2026-09-29 to 2026-10-01): 3
- Combined diagnostic signals: 10

## Recent fixed one-per-day cohort

| Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed | LOTO robust |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| CURRENT_BASELINE | -2946 | +0 | 1/2 | 0.586352 | -3561 | 7122 | 0 | NO |
| EARLY_FAILURE_30S | -5241 | -2295 | 0/3 | 0.0 | -1747 | 5241 | 1 | NO |
| EARLY_FAILURE_60S | 1045 | +3991 | 1/2 | 1.333759 | -1566 | 3131 | 0 | YES |
| EARLY_FAILURE_90S | -451 | +2495 | 1/2 | 0.902529 | -2314 | 4627 | 0 | YES |
| EARLY_FAILURE_120S | -1949 | +997 | 1/2 | 0.681796 | -3062 | 6125 | 0 | NO |
| EARLY_FAILURE_150S | -2946 | +0 | 1/2 | 0.586352 | -3561 | 7122 | 0 | NO |
| EARLY_FAILURE_180S | -2946 | +0 | 1/2 | 0.586352 | -3561 | 7122 | 0 | NO |
| EARLY_FAILURE_240S | -2946 | +0 | 1/2 | 0.586352 | -3561 | 7122 | 0 | NO |
| EARLY_FAILURE_300S | -2946 | +0 | 1/2 | 0.586352 | -3561 | 7122 | 0 | NO |

## Older independent-long cohort

| Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed | LOTO robust |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| CURRENT_BASELINE | -6681 | +0 | 1/6 | 0.521281 | -2326 | 10272 | 0 | NO |
| EARLY_FAILURE_30S | -12064 | -5383 | 0/7 | 0.0 | -1723 | 12064 | 1 | NO |
| EARLY_FAILURE_60S | -12365 | -5684 | 0/7 | 0.0 | -1766 | 12365 | 1 | NO |
| EARLY_FAILURE_90S | -12565 | -5884 | 0/7 | 0.0 | -1795 | 12565 | 1 | NO |
| EARLY_FAILURE_120S | -2488 | +4193 | 1/6 | 0.74516 | -1627 | 8274 | 0 | YES |
| EARLY_FAILURE_150S | -3986 | +2695 | 1/6 | 0.646035 | -1877 | 9572 | 0 | YES |
| EARLY_FAILURE_180S | -4785 | +1896 | 1/6 | 0.603234 | -2010 | 10371 | 0 | NO |
| EARLY_FAILURE_240S | -5282 | +1399 | 1/6 | 0.579358 | -2093 | 10469 | 0 | NO |
| EARLY_FAILURE_300S | -7377 | -696 | 1/6 | 0.496519 | -2442 | 12365 | 0 | NO |

## Combined matched diagnostic

| Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed | LOTO robust |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| CURRENT_BASELINE | -9627 | +0 | 2/8 | 0.543268 | -2635 | 17394 | 0 | NO |
| EARLY_FAILURE_30S | -17305 | -7678 | 0/10 | 0.0 | -1730 | 17305 | 2 | NO |
| EARLY_FAILURE_60S | -11320 | -1693 | 1/9 | 0.269489 | -1722 | 15496 | 1 | NO |
| EARLY_FAILURE_90S | -13016 | -3389 | 1/9 | 0.242904 | -1910 | 17192 | 1 | NO |
| EARLY_FAILURE_120S | -4437 | +5190 | 2/8 | 0.720733 | -1986 | 14399 | 0 | YES |
| EARLY_FAILURE_150S | -6932 | +2695 | 2/8 | 0.622912 | -2298 | 16694 | 0 | YES |
| EARLY_FAILURE_180S | -7731 | +1896 | 2/8 | 0.596966 | -2398 | 17493 | 0 | NO |
| EARLY_FAILURE_240S | -8228 | +1399 | 2/8 | 0.581889 | -2460 | 17591 | 0 | NO |
| EARLY_FAILURE_300S | -10323 | -696 | 2/8 | 0.525902 | -2722 | 19487 | 0 | NO |

## Finding

- 60 seconds looked positive on the recent three trades (1045 TWD) but failed the expanded matched cohort (-11320 TWD) and harmed one of two baseline winners.
- 120 seconds gave the best combined net result (-4437 TWD; +5190 versus baseline) and its improvement survived every leave-one-trade-out deletion.
- The 120-second result still has negative total PnL and PF below 1. It is evidence for loss reduction, not evidence of a profitable rule.
- No checkpoint should be promoted to production from this sample. The next valid step is shadow collection on complete sessions with 0050 context.

## Limitations

- The 20260922-20260924 archive predates permanent 0050 collection.
- Older records are overlapping independent signals, not a realizable single-position portfolio.
- 20260922 started at 09:30; 20260923 started at 09:19 and has 24 callback errors.
- The combined total is a signal-level diagnostic and must not be read as account PnL.

No production or live behavior changed.
