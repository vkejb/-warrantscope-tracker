# STRICT_MARKET_WIDE_V1 replay report

## Boundary

The policy was frozen before reading replay performance. It is a post-session,
causal, long-only shadow replay over the 16 existing expanded-universe archives
from 2026-10-06 through 2026-11-09. These are known exploratory dates, not a new
out-of-sample period. No scheduler, production policy, order adapter, broker
session, or live setting was changed.

Policy hash:
`1e88bdc7894284c1b50a4fa916fdf4d4b840a226c3026abe43bd3df187deddd4`

## Result

- Sessions: 16
- Production-engine approved first-per-symbol signals: 51 across 12 sessions
- Strict confirmations: 0
- Trades / scored trades: 0 / 0
- Net P&L: TWD 0, which is the cash baseline rather than evidence of an edge
- Win rate and profit factor: NA because no trade occurred
- Actual orders / fills / broker calls: 0 / 0 / 0

The main rejection components overlap:

- after-cost progress at the executable bid was not positive: 37
- 60-second signed flow turned negative: 16
- delayed entry exceeded the existing VWAP anti-chase limit: 9
- latest tick was stale: 8
- breakout was no longer held: 7
- executable bid was below causal VWAP: 6
- delayed entry exceeded the existing opening anti-chase limit: 5
- spread was too wide: 3
- bearish market context at signal or confirmation: 2

Only two signals had positive hypothetical after-cost progress at 60 seconds:
3701 on 2026-10-20 (TWD 3,593) and 6234 on 2026-10-30 (TWD 623). Both were
rejected because the delayed entry was already beyond both production
anti-chase limits. They are diagnostic cases, not trades and not a basis for
removing those limits after seeing the outcome.

## Conclusion

This exact all-gates-at-once version is too restrictive for the observed
archive: it removes all trades, so it cannot demonstrate higher win rate or
positive expectancy. It must not be promoted or connected to formal orders.
The useful finding is attribution: the after-cost 60-second progress gate is
the dominant bottleneck, while the market filter is not.

To avoid overfitting, do not loosen a threshold using these same 16 dates and
then describe the result as validation. Preserve this failed version. A next
version, if authorized, should be frozen as a small ablation (one gate removed
at a time), retain the cash baseline and full candidate ledger, and be judged
on newly collected dates. Signal count, win rate, and P&L must be considered
together; a zero-trade strategy has neither a win rate nor demonstrated value.

Local generated evidence (Git-ignored):

- `dynamic_market_scanner_shadow_v01/strict_results/candidate_ledger.csv`
- `dynamic_market_scanner_shadow_v01/strict_results/sessions.csv`
- `dynamic_market_scanner_shadow_v01/strict_results/trades.csv`
- `dynamic_market_scanner_shadow_v01/strict_results/summary.json`
- `dynamic_market_scanner_shadow_v01/strict_results/run_manifest.json`
