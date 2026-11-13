# Fixed take-profit study v0.1 — frozen specification

Status: `SHADOW_ONLY`, `NO_LIVE_ORDER`, `actual_orders=0`.  This module is not
imported by the live runtime and must not change the production exit policy.

The cohort is the immutable `PRODUCTION_ANTI_CHASE` paper entry (at most one
entry per day) from every verified expanded-universe paper day available when
the study is run.  Entry time, price and quantity are held fixed across exit
variants.  Days without an entry remain in daily results with zero PnL.  This
is an exploratory, already-observed archive; it is not out-of-sample evidence.

All variants retain the same NT$3,500 net hard stop, causal reversal check,
13:20 hard exit, fresh executable-side quote requirement, fees and tax.  The
only changed component is profit protection:

* `CURRENT_MFE_V1`: the sealed production-paper result using the current MFE
  policy (1R arms breakeven; from 1.5R/2R/3R retain 60%/70%/75%).
* `FIXED_NET_1R`: replace MFE protection with an exit when executable net PnL
  first reaches NT$3,500.
* `FIXED_NET_1_5R`: replace MFE protection with an exit when executable net PnL
  first reaches NT$5,250.
* `HYBRID_EARLY_FLOOR`: retain `CURRENT_MFE_V1`, and additionally, after
  executable net MFE first reaches 0.75R (NT$2,625), exit if executable net PnL
  falls to 0.30R (NT$1,050) or below.

The 0.75R/0.30R hybrid is an existing documented shadow hypothesis, not a value
selected from this study's outcome.  No grid search or per-stock tuning is
allowed.  A trigger is evaluated only from a fresh causal liquidation quote;
the same quote is the simulated full fill.  Queue position and market impact
remain unknown, so `STRICT_EXECUTABLE=NA`.

Primary comparison uses the archived execution proxy.  An exit-only stress
scenario delays a trigger by one second and then requires the next fresh quote;
it does not re-price or re-select entries and is therefore sensitivity evidence,
not a complete stressed account replay.

The decision rule is descriptive: compare after-cost PnL, PF, maximum drawdown,
profit giveback and result excluding the best trade.  A higher win rate alone,
or improvement driven by one trade/day, is insufficient to change production.
