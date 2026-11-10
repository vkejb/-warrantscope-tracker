# FLOW_REVERSAL_COST_AWARE_BREAKEVEN_V1

Frozen at 2026-11-10 17:04:40 Asia/Taipei, before computing this study's
performance.  This is a broker-free known-archive diagnostic.  It changes no
entry, production exit, scheduler, or live setting.

## Cohort and comparison

- Reuse the frozen expanded-universe `V2_RECENT_VOLUME_ACCELERATION` daily
  selection and its BASE and STRESS entry fills.
- Keep the same selected stock, entry time, price, quantity, cost model, and
  baseline exit for every paired comparison.
- The known 16-session archive is exploratory.  The 2026-11-10 freeze day is
  reported separately and is not renamed as prospective evidence.
- No threshold is selected from the resulting performance.

## Fixed exit variants

- `BASELINE`: unchanged production exit.
- `FLOW_BE_ANY_POSITIVE`: protection is eligible after any strictly positive
  cost-after-fees-and-tax executable MFE.
- `FLOW_BE_0_25R`: protection is eligible after executable net MFE reaches
  0.25R.  R is the unchanged TWD 5,000 production stop, so the threshold is
  TWD 1,250.

At 30-second causal decision times after entry, compute:

1. 60-second signed active-volume delta; adverse at `<= -0.35`.
2. 60-second large-trade delta, with the large threshold fixed at the causal
   90th percentile of the preceding 300 seconds; adverse at `<= -0.25`.
3. Latest fresh five-level book imbalance; adverse at `<= -0.10`.

A flow reversal requires at least two of the three adverse checks at two
consecutive decision times.  The thresholds, 60/300-second windows, 30-second
decision interval, freshness limits, and two confirmations are inherited from
the existing direction engine; they are not searched here.

After flow reversal activates protection, the first later executable
liquidation quote whose projected net P&L is `<= 0` exits with reason
`FLOW_REVERSAL_COST_AWARE_BREAKEVEN`.  The quote uses the existing adverse-tick
exit proxy.  If the baseline exits first, the baseline result remains.  A
missing or stale flow snapshot never becomes an adverse vote.

## Required diagnostics

- Paired net-P&L delta, wins/losses/breakevens, PF, mean/median trade, worst
  trade/day, and net after removing the best trade/day.
- Daily equity and maximum drawdown from TWD 190,000.
- Activation and protective-exit counts, MFE giveback before/after, losses
  avoided, profits surrendered, and later recovery after a protective exit.
- Missing flow, unfilled entry, and unscorable exit counts remain explicit.
- `actual_orders=0`, `actual_fills=0`, `broker_order_calls=0` in every output.

No result from this study enables live trading.  A useful retrospective result
must still be frozen and validated on later untouched full sessions.
