# Frozen V2 volume acceleration on the expanded universe

## Scope

The 30-second V2 acceleration parameters come unchanged from the older
`volume_flow_entry_study_v01`; they were not fitted to these 51 expanded-pool
signals. Application to the 16 expanded archives is still exploratory because
the archives were already known.

Policy hash:
`32150ce741a26aa47e68849983cbae6156e7b18573f07cc4afa208da4865bf67`

## Results

| Variant / execution | Selected | Scored | W-L | Win rate | Net P&L | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|
| V0 base | 12 | 12 | 4-8 | 33.3% | TWD -12,378 | 0.450 | TWD 15,990 |
| V0 5s delayed/adverse quote | 12 | 8 | 2-6 | 25.0% | TWD -4,576 | 0.786 | TWD 11,555 |
| V2 base | 9 | 9 | 3-6 | 33.3% | TWD -5,732 | 0.619 | TWD 8,841 |
| V2 5s delayed/adverse quote | 9 | 6 | 3-3 | 50.0% | TWD 9,538 | 1.940 | TWD 6,223 |

The delayed scenario is not a monotonic guaranteed-worse fill. It waits five
seconds, then uses the next eligible ask plus two ticks; the market may have
moved lower or higher. Three V2 selections had no eligible tick within the
freshness bound and remain unfilled/NA, not zero-P&L observations.

The positive V2 delayed result is concentrated. Its best trade, 4960 on
2026-10-19, earns TWD 12,008. Removing it changes the remaining result to
TWD -2,470. The base scenario remains negative. Six scored delayed trades are
also below the pre-existing ten-trade threshold.

## Decision

V2 is the first expanded-pool condition worth retaining for new-date shadow
observation because its parameters predate this application and it improves the
observed delayed scenario. It is not approved for UAT or live trading.

Prospective retention rules are frozen separately. At minimum, unseen dates
must produce ten scored trades across five trading dates; both base and delayed
tracks must have positive net P&L and PF above one, each must remain positive
after removing its best trade, and missing/unfilled outcomes must remain in the
denominator and evidence ledger. Failure means rejection, not threshold tuning.

Local generated evidence (Git-ignored):

- `dynamic_market_scanner_shadow_v01/expanded_volume_gate_results/signals.csv`
- `dynamic_market_scanner_shadow_v01/expanded_volume_gate_results/trades.csv`
- `dynamic_market_scanner_shadow_v01/expanded_volume_gate_results/experiments.csv`
- `dynamic_market_scanner_shadow_v01/expanded_volume_gate_results/run_manifest.json`
