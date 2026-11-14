# Entry winner/loser ablation v0.1 — frozen before account replay

Scope: archived expanded-universe LONG research only. `SHADOW_ONLY`,
`NO_LIVE_ORDER`, `actual_orders=0`. No live policy or scheduled job imports this
study. All dates through 2026-11-13 have already been observed and are
exploratory, not out-of-sample.

Two cohorts must remain distinct:

1. The 20 verified expanded paper days contain 18 `PRODUCTION_ANTI_CHASE`
   selected entries. Compare their entry-time features by realized sign under
   the unchanged MFE exit. Screening these sealed selections against cash is
   a diagnostic only: it cannot replace a rejected first trade with a later
   candidate and must not be called a full account replay.
   Post-entry MFE, MAE, holding time and exit reason are reported separately;
   they cannot become entry filters.
   Its unchanged control is labelled `PAPER_UNCHANGED`, since this cohort did
   not use the separate `CURRENT_RS1_180S` gate.
2. The separate frozen `CURRENT_RS1_180S` early-ready candidate universe has
   18 days and 365 first-per-symbol candidates. Reuse its unchanged 1% RS and
   positive after-cost 180-second progress gate. Apply each extra gate once,
   before ranking the eligible candidates by earliest confirmation, then
   higher RS, then stock ID. Replay at most one actual paper entry per day
   using the unchanged 190,000 TWD, whole-lot sizing, fee/tax and MFE exit.
   This is a full account selection replay within this *research* cohort, not
   the current production paper cohort.

The sealed paper record has a 60-second `volume_delta`, not a 120-second
entry-flow field. Therefore its `FLOW_120S_0_5` screening cell is NA; the
120-second gate is evaluated only in the candidate archive with causal ticks.

The fixed one-at-a-time additions are:

| Gate | Rule | Reason |
|---|---|---|
| `MARKET_BULLISH` | signal-time 0050 regime is BULLISH | Selected paper winners were all bullish; inspect contradiction with all-candidate labels. |
| `OPENING_EXT_1PCT` | confirmed entry/opening price - 1 <= 1% | Half the current 2% anti-chase cap; tests late chasing. |
| `SPREAD_30BPS` | fresh confirmation book spread <= 30 bps | Fixed tighter cost/quoteability check versus current 40 bps. |
| `FLOW_120S_0_5` | signal-time signed 120-second volume delta >= 0.50 | Prior small production-A diagnostic suggested sustained buy flow; SDK direction semantics remain conditional. |
| `RS_2PCT` | signal-time stock minus 0050 300-second return >= 2% | Existing quality table's predefined >=2% bucket; audits the opportunity cost of stronger RS. |

No combinations, threshold search, or per-stock exceptions. The base and the
same five-second/two-tick entry stress scenario are both replayed. Account
exits use the current archived fresh-quote proxy. A signal filtered out can
release the daily slot for another eligible signal; missing or unscorable
execution stays visible. All qualified signals are diagnostic and overlapping;
only one-trade-per-day rows constitute research account P&L. Days with no
eligible entry contribute zero. Every gate is compared with cash and the same
base over identical 18 dates. Preserve rejected winners and losers using the
same candidate's fixed 30-minute after-cost mark. Flow direction uses the
existing archived flag/midpoint proxy and does not certify SDK semantics.

Decision threshold: no production promotion from this archive. A candidate
needs new unseen dates and stable BASE and stress performance, including
losses, missed winners, maximum drawdown and concentration. The 18 selected
paper entries and 365 early-ready candidates are not independent tests.
