WarrantScope SHORT side study V0.1 — offline only

From stock-strategy:
  python3 -m short_side_study_v01.main --audit-only
  python3 -m short_side_study_v01.main
  python3 -m short_side_study_v01.verify

The module only reads quote archives and imports the existing strategy as a
library. It never imports or calls broker login/order functions, changes LIVE
configuration, or writes existing runtime directories.

The current default results directory is results_expanded. The original
four-day results are preserved under results. Archive discovery scans all
collector archive directories in Downloads, including other checkouts and
incident/recovery copies; --archive-root can select additional search roots.
ZIP directory catalogs are also audited. Selection is based on quality and
full-session coverage, never on whether LONG previously traded or made money.
daily_inventory.json lists selected and rejected sources for every date.

The entry selector fixes S0 LONG A, S1 diagnostic SHORT without regime gate,
S2 symmetric SHORT regime gate, and chronological S3. The first complete approved
signal consumes the daily one-trade attempt; same-boundary ties use the unchanged
score/notional ordering. All qualified boundaries and simultaneous cases are
retained independently of realized outcomes.

The exit engine is inherited unchanged, including direction-aware -3500 stop,
MFE_V1, reversal confirmation, and 13:20 hard exit. Stale quotes cannot fabricate
exit fills. Recorded five-level capacity caps every fill; repeated processing
cannot reuse the same snapshot capacity. Residual exposure retains null PnL.

Displayed capacity is not a guarantee of queue fill. Identical callbacks never
replenish used liquidity: only a positive observed displayed-size increment
at the same price restores capacity. Hidden queues and exchange matching are
not modeled. Partial entry remainder is canceled immediately in the replay; real
broker cancel latency is not available. Minimum per-side fees and sell tax are
unchanged from the frozen model. Both daily realized drawdown and quote-marked
intraday account drawdown are reported. Quote marks combine prior completed
exit value with remaining shares; they do not assert enough depth to liquidate.
MFE/MAE/giveback use the same net cost model and retained causal quote marks.
An incomplete account has null net_pnl, plus explicitly labeled completed-subset
figures; missing exposure is never substituted with zero.

SPARK depth volume units have not been independently proved; replay includes
1000 shares/unit and a conservative 1 share/unit sensitivity. These multipliers
are assumptions, not optimized choices. The original S0 full-fill reference is
stored separately so adding the required capacity cap does not silently replace
the original model. A partial fill that cannot define the unchanged -3500/MFE
basis is unscorable, never assigned a fabricated R or completed PnL.

TWSE historical TWTB4U flags are cached under sources and applied before selecting
SHORT signals. TPEX historical eligibility and broker-specific entitlement are
unproven. Historical qualification does not establish live availability.

All results are SIGNAL_ONLY / ELIGIBILITY_UNPROVEN and
NO_TRUE_OUT_OF_SAMPLE_SET. Do not use this research to enable LIVE SHORT.
No parameter search, threshold fit, publication, merge, or deployment is included.

Primary artifacts: contract.json, data_inventory.json, daily_results.json,
s0_original_full_fill_reference.json, qualified_signals.json,
simultaneous_cases.json, eligibility_audit.json, summary.json,
report_zh-TW.txt, tests.txt, validation_manifest.json.
Expanded artifacts also include daily_inventory.json, zip_catalog.json,
signal_counts.json, and stream_quality.json. SHORT signal counts refer to stock
per 30-second decision boundaries; continuing signals can be counted repeatedly.
Model entry count includes open/unscorable entries, while completed-trade count
and win/PF statistics use only completed positions. No entries are real orders.

S4 controlled account extension (same six-day frozen reference cohort):
  python3 -m short_side_study_v01.s4
  python3 -m short_side_study_v01.s4_verify

This extension writes results_s4 without changing prior results. It feeds LONG A
and raw SHORT S1 into separate approval/confirmation states on each chronological
boundary. The first complete approval consumes the daily attempt, even when its
entry is unfilled; later signals and losing-trade outcomes cannot reset it.
Only simultaneous approvals compete by existing score/notional ordering.

S4 source selection occurs before any PnL calculation. All displaced standalone
trades are separately labeled counterfactuals and are never added to the account.
S1 0050 regimes are descriptive metadata at the approved-signal timestamp, not
new gates; missing regime data is UNKNOWN and does not block raw SHORT.

results_s4 includes comparisons for both 1000 and 1 share/unit assumptions,
daily choices, counterfactual replacements, incremental attribution, all three
execution scenarios, extreme-trade removals and leave-one-day-out. Baseline
parity checks compare all 72 S0/S1 daily-scenario-unit outcomes to results_expanded.
All S4 outputs remain UNIT_UNVERIFIED / ELIGIBILITY_UNPROVEN /
NO_TRUE_OUT_OF_SAMPLE_SET. No SHORT broker execution layer is implemented here.
