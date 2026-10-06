# Three-minute continuation and net-MFE diagnostic

This is a backtest-only controlled comparison. No LIVE or broker behavior changed.

| Variant | Evaluable (unscorable) | Entered/rejected | W/L | Win rate | Net PnL | Matched baseline | Delta | Exp/opportunity | PF | Avg winner | Avg loser | Max DD | Winners rejected | LOO robust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| BASELINE | 17 (0) | 17/0 | 6/11 | 35.3% | -1,778 | -1,778 | 0 | -105 | 0.93 | 4,050 | -2,371 | 9,664 | 0 | NO |
| CONFIRM_3M | 6 (11) | 3/3 | 2/1 | 66.7% | 5,260 | 6,017 | -757 | 877 | 2.48 | 4,408 | -3,555 | 3,555 | 0 | NO |
| COST_AWARE_BREAKEVEN | 17 (0) | 17/0 | 6/11 | 35.3% | -680 | -1,778 | 1,098 | -40 | 0.97 | 4,050 | -2,271 | 9,664 | 0 | YES |
| CONFIRM_3M_PLUS_COST_AWARE_BREAKEVEN | 6 (11) | 3/3 | 2/1 | 66.7% | 5,260 | 6,017 | -757 | 877 | 2.48 | 4,408 | -3,555 | 3,555 | 0 | NO |

## Confirmation rejection reasons

- NET_PROGRESS_NOT_POSITIVE: 2
- NET_PROGRESS_NOT_POSITIVE+BREAKOUT_NOT_HELD+VWAP_NOT_HELD+VOLUME_FLOW_REVERSED+LARGE_TRADE_FLOW_REVERSED: 1

## Unscorable data reasons

- QUOTE_STALE: 11

## Limitations

- Only seventeen scorable independent signals across three historical sessions are available.
- All three source sessions are partial and 20260923 contains callback errors.
- Signals overlap and therefore are not one simultaneously executable 190k portfolio.
- The 180-second rule is fixed from the stated hypothesis and was not parameter-optimized.
- Rejected trades contribute zero PnL to opportunity expectancy; entry expectancy is also reported separately.
