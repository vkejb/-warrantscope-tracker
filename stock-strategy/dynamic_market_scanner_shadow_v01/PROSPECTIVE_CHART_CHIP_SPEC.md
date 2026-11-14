# PROSPECTIVE_CHART_CHIP_V1 — frozen 2026-11-15

Mode: SHADOW_ONLY, NO_LIVE_ORDER, actual_orders=0. This is a data and label study, not a new trading strategy. It does not modify Stage A, the production LONG rule, order adapter, subscription, or schedule.

## Information time

- Institutional source day must be the trading day immediately preceding the target session. Capture both TWSE T86 and TPEx institutional raw JSON in an independent immutable research folder, with download completion time, URL, source date, parsed row count and SHA-256. The capture must finish before target-session 09:00 Taipei to qualify as `PRESIGNAL_CAPTURE`; otherwise `LATE_OR_UNKNOWN`. A source that is missing, mismatched, or unparseable is `UNKNOWN`, never zero flow.
- The 18 days of 2026-10-06–2026-11-11 are already inspected exploratory dates. Their official flow files were downloaded later; they remain `HISTORICAL_RECONSTRUCTION`, even though the rule uses the preceding day's row. No old result may be renamed out-of-sample.
- For actual prospective validation, the target session's candidate-universe seal, the immediately preceding daily OHLCV bar, both official source captures, and quote run manifest must each be present and hashed. Missing components make the applicable feature or outcome `NA`, not a failed signal.

## Separate outcomes, no new gates

Population: first-per-symbol early-ready signals that pass unchanged `CURRENT_RS1_180S`. Keep the existing 190,000 TWD, one-trade-per-day account selection separate from all-signal diagnostics.

At the original simulated entry time, use the existing ask-plus-one-tick and whole-lot quantity. At 5/15/30 minutes, report (a) latest fresh trade-price change from entry, (b) fresh best-bid round-trip change and (c) original model's net P&L including costs. Quote freshness uses the existing signal-outcome policy; stale or absent marks are `NA`. Do not use a later quote to repair a missing mark. Original rule-exit P&L remains a fourth, distinct label; do not sum repeated horizons or all signals into an account return.

## Fixed hypotheses for future, not current selection

H1: prior-session combined institutional net shares sign, with TWSE/TPEx components preserved; baseline comparison includes both positive and nonpositive names. It is a risk annotation, not a live hard veto.

H2: count of positive combined-flow sessions in the preceding three valid trading sessions. Require all three exact-date official captures; otherwise `NA`. No magnitude threshold is selected on the already seen dates.

H3: prior close relative to 5-day mean and 5-day mean relative to 20-day mean as continuous chart descriptors. The old binary `close>MA5>MA20` is retained only as a diagnostic. Do not derive a new overextension threshold from these 18 dates.

Daily-volume-normalized chip flow remains `NA` until the daily volume unit and matching share class are evidenced; no unverified shares-versus-lots division. A breakout/retest pattern requires separately frozen causal intraday definitions and is not claimed tested here.

For future evaluation, first assess data completeness and price/entry/exit labels, then H1–H3 by whole trading day. Compare a frozen, shared-capital candidate-order counterfactual against existing LONG and cash only after enough truly new sessions exist. Keep all no-signal and losing days. Do not tune on the first prospective batch and call it out-of-sample. Existing stress unscored orders remain `NA`.
