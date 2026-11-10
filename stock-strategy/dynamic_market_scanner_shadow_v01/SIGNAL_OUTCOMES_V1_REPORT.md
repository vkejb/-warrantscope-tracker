# Expanded approved-signal outcome report

## Scope

This report covers all 51 first-per-symbol production-engine-approved signals
found across 12 of the 16 expanded-universe archive sessions from 2026-10-06
through 2026-11-09. Checkpoints and group boundaries were frozen before the
outcomes were read. All numbers are after the existing modeled round-trip costs
and use the latest non-stale best bid already received by the checkpoint.

Policy hash:
`2357b4712490317838cc71c621797e874696c14cf7d450aa73721986657e1672`

## Fixed-horizon results

| Entry track | Horizon | Evaluable | Positive | Rate | Mean net P&L | Median net P&L |
|---|---:|---:|---:|---:|---:|---:|
| Original signal | 1m | 51 | 2 | 3.9% | TWD -1,036 | TWD -978 |
| Original signal | 3m | 51 | 8 | 15.7% | TWD -914 | TWD -1,017 |
| Original signal | 5m | 51 | 10 | 19.6% | TWD -901 | TWD -1,126 |
| Original signal | 15m | 48 | 11 | 22.9% | TWD -679 | TWD -1,160 |
| Original signal | 30m | 48 | 14 | 29.2% | TWD -952 | TWD -1,961 |
| Delayed 60s | 1m | 39 | 1 | 2.6% | TWD -1,219 | TWD -1,203 |
| Delayed 60s | 5m | 39 | 6 | 15.4% | TWD -1,059 | TWD -1,402 |
| Delayed 60s | 30m | 38 | 9 | 23.7% | TWD -1,077 | TWD -1,919 |

The diagnostic sums are negative at every checkpoint. They are not account P&L
because signals overlap and ignore the shared one-trade capital limit.

## Structural diagnostics

No frozen descriptive group has a positive median 30-minute result. The only
group with a positive aggregate is the TWD 50-100 entry-price group: TWD 2,948
across 20 evaluable signals, but its median is TWD -1,717. Its best date,
2026-10-19, contributes TWD 20,117; removing that date changes the group to
TWD -17,169. It is therefore concentration, not robust evidence for a new
price filter.

At 30 minutes, the bullish-market group loses TWD 37,624. Excluding its best
date changes it to TWD -61,762. Positive versus non-positive 60-second flow
also leaves both groups negative. Thus market state, price bucket, time bucket,
and flow sign do not supply a stable standalone rule in these data.

Selecting the highest production score after seeing the whole day would lose
TWD 12,768 over 12 dates. Selecting the highest relative-strength signal after
seeing the whole day would show TWD 2,175, but that selection is non-causal: at
an early signal the system cannot know whether a stronger one will arrive
later. It is retained only as a hypothesis for a pre-declared causal auction,
not as strategy performance.

## Decision

The expanded pool successfully increases the number of candidates, but the
current breakout signal does not have positive after-cost short-horizon quality.
More filters on the same immediate-entry event are unlikely to solve this by
themselves. No result here is approved for production or prospective promotion.

The next bounded hypothesis should change selection timing rather than fit a
numeric threshold: collect candidates within a pre-declared causal window,
rank only candidates known by its close, revalidate the quote, and replay one
shared-capital trade. The window and ranking rule must be frozen before running
the known archive, then retained only if later unseen dates support it.

Local generated evidence (Git-ignored):

- `dynamic_market_scanner_shadow_v01/signal_outcome_results/signal_outcomes.csv`
- `dynamic_market_scanner_shadow_v01/signal_outcome_results/horizon_metrics.csv`
- `dynamic_market_scanner_shadow_v01/signal_outcome_results/daily_diagnostics.csv`
- `dynamic_market_scanner_shadow_v01/signal_outcome_results/feature_diagnostics.csv`
- `dynamic_market_scanner_shadow_v01/signal_outcome_results/ranking_diagnostics.csv`
- `dynamic_market_scanner_shadow_v01/signal_outcome_results/run_manifest.json`
