# MFE profit protection diagnostic

This is a backtest/shadow-only comparison. Live strategy and order routing are unchanged.

| Variant | Trades | Win rate | Net PnL | Expectancy | PF | Max DD | Avg winner | Avg loser | MFE exits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASELINE | 1 | 100.00% | 911.00 | 911.00 | N/A | 0.00 | 911.00 | N/A | 0 |
| MFE_LOOSE | 1 | 100.00% | 911.00 | 911.00 | N/A | 0.00 | 911.00 | N/A | 0 |
| MFE_V1 | 1 | 100.00% | 911.00 | 911.00 | N/A | 0.00 | 911.00 | N/A | 0 |
| MFE_AGGRESSIVE | 1 | 100.00% | 911.00 | 911.00 | N/A | 0.00 | 911.00 | N/A | 0 |

## Data and execution limits

- Sessions accepted by the existing production-parity coverage gate: 1
- Scored production-parity entries: 1
- Execution model: `IMMEDIATE_FULL_FILL_AT_LIVE_ADVERSE_ONE_TICK_PROXY`
- Entry selection, timing, sizing, existing exits, fees and tax are unchanged.
- MFE uses the existing executable liquidation-quote proxy, not an optimistic raw last-trade high/low.
- Replay still assumes immediate full entry/exit fills at the existing adverse-one-tick proxy; queue position, latency and real partial fills are not observed.
- Source data are ticks, so intrabar ambiguity is zero in this run; OHLC ambiguity is handled and unit-tested for future bar inputs.

## Diagnostic answers

```json
{
  "1_large_mfe_low_retention_frequency": {
    "large_mfe_trade_count": 0,
    "below_50pct_retention_count": 0,
    "supported": false
  },
  "2_low_retention_cause_of_poor_expectancy": "NOT_IDENTIFIABLE_FROM_AVAILABLE_SAMPLE",
  "3_profit_factor_improved": {
    "MFE_LOOSE": null,
    "MFE_V1": null,
    "MFE_AGGRESSIVE": null
  },
  "4_expectancy_improved": {
    "MFE_LOOSE": false,
    "MFE_V1": false,
    "MFE_AGGRESSIVE": false
  },
  "5_maximum_drawdown_reduced": {
    "MFE_LOOSE": false,
    "MFE_V1": false,
    "MFE_AGGRESSIVE": false
  },
  "6_average_winner_reduction": {
    "MFE_LOOSE": 0.0,
    "MFE_V1": 0.0,
    "MFE_AGGRESSIVE": 0.0
  },
  "7_largest_winners_prematurely_exited": 0,
  "8_best_tradeoff": "NO_OBSERVED_DIFFERENCE",
  "9_results_after_costs": true,
  "10_breadth": {
    "mfe_exit_count": 0,
    "unique_trades": 0,
    "interpretation": "NO_MFE_EXITS"
  }
}
```

## Conclusion

No available production-parity trade reached the 1R activation threshold before its existing exit. All four variants are therefore identical in this sample, and no configuration can be recommended.
