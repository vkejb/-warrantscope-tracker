# STRICT_MARKET_WIDE_V1 ablation report

## Scope

This report uses the 16 already-known expanded-universe archives from
2026-10-06 through 2026-11-09. Twelve sessions contained at least one parent
candidate. The test changes no numeric threshold and removes only one named
gate or policy family at a time. All results remain exploratory.

Policy hash:
`8e61e35e57a865b1c08f2c99ff9171f6531e6fcffbaa179ee17f000297cb8bce`

## Results

| Variant | Trades | W-L | Win rate | Net P&L | PF | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|
| A0 all strict gates | 0 | NA | NA | TWD 0 | NA | TWD 0 |
| A1 remove net-progress only | 7 | 1-6 | 14.3% | TWD -11,835 | 0.293 | TWD 11,835 |
| A2 remove flow only | 0 | NA | NA | TWD 0 | NA | TWD 0 |
| A3 remove anti-chase family only | 2 | 0-2 | 0% | TWD -3,983 | 0 | TWD 3,983 |

A1 restored one trade on each of seven dates. Six lost money. The sole winner
was 2489 on 2026-10-19 at TWD 4,893; the remaining six produced TWD -16,728,
so the result is not a small-cost effect or a broad improvement.

A3 admitted the two cases that had positive hypothetical progress from the
original signal price. Once entry was correctly delayed until after the
60-second observation, 3701 on 2026-10-20 lost TWD 409 and 6234 on 2026-10-30
lost TWD 3,574. The earlier mark-to-market progress was not an executable
future edge from the delayed entry.

## Decision

None of A1, A2, or A3 is retained as a prospective candidate. Removing the
cost-progress or anti-chase gate increases signal count but worsens after-cost
P&L and drawdown; removing flow alone does not restore any signal. The evidence
does not support lowering these gates or connecting them to production.

The next useful research step is not another threshold search on these same
dates. It is a causal outcome table for all approved expanded-universe signals,
including fixed 1/3/5/15/30-minute executable returns and path diagnostics,
followed by a new pre-frozen structural hypothesis. Any candidate must then be
judged on newly collected dates and against cash and the unchanged production
baseline.

Local generated evidence (Git-ignored):

- `dynamic_market_scanner_shadow_v01/strict_ablation_results/selected_signals.csv`
- `dynamic_market_scanner_shadow_v01/strict_ablation_results/trades.csv`
- `dynamic_market_scanner_shadow_v01/strict_ablation_results/experiments.csv`
- `dynamic_market_scanner_shadow_v01/strict_ablation_results/summary.json`
- `dynamic_market_scanner_shadow_v01/strict_ablation_results/run_manifest.json`
