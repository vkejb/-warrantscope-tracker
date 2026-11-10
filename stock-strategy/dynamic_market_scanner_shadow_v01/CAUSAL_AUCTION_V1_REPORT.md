# Five-minute causal auction report

## Scope

This is the single pre-frozen five-minute relative-strength auction described in
`CAUSAL_AUCTION_V1_SPEC.md`. Every replay decision is causal, but the hypothesis
was designed after the 16 archive sessions were inspected; the result is
therefore exploratory, not out-of-sample.

Policy hash:
`cf4702bf3c3fcae0ae97a7ccd1025ee0a6d94cffb83ed049b5148237247c717b`

## Account comparison

| Strategy | Trades | W-L | Win rate | Net P&L | PF | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|
| Expanded production first-signal baseline | 14 | 3-11 | 21.4% | TWD -26,374 | 0.198 | TWD 29,986 |
| Five-minute causal RS auction | 1 | 0-1 | 0% | TWD -3,598 | 0 | TWD 3,598 |

The smaller loss and drawdown are caused by almost complete inactivity, not a
demonstrated improvement in selection. Across 12 sessions with parent signals,
33 five-minute windows were evaluated. Thirty-one had no candidate still
passing the full production entry engine at window close, one had stale/missing
0050 context, and one produced an entry.

The sole trade was 2426 on 2026-10-16: three candidates entered the 09:10-09:15
window, one revalidated at 09:15, and it was entered at TWD 94 for 2,000 shares.
It reached modeled MFE of TWD 1,990 but eventually stopped at TWD 92.5 for a
net loss of TWD 3,598.

## Decision

Do not retain this auction as a prospective strategy. It fails both signal-count
and after-cost performance requirements. Extending the window or weakening the
close revalidation on these same dates would be another post-hoc search and is
not authorized by this result.

Together with the all-signal outcome table, this indicates that the current
breakout event usually disappears quickly and lacks cost-adjusted persistence.
The next defensible step is to collect new full-day candidate/outcome evidence
prospectively while a genuinely different base-entry hypothesis is specified;
it is not to keep varying the window until the archive turns positive.

Local generated evidence (Git-ignored):

- `dynamic_market_scanner_shadow_v01/causal_auction_results/trades.csv`
- `dynamic_market_scanner_shadow_v01/causal_auction_results/auction_diagnostics.csv`
- `dynamic_market_scanner_shadow_v01/causal_auction_results/comparison.csv`
- `dynamic_market_scanner_shadow_v01/causal_auction_results/run_manifest.json`
