# STAGE_A_T1_EXTREME_UPSIDE_STUDY_V0_1

Research-only next-session diagnostic of the published frozen Stage A daily Top30. No broker connection, order, warrant overlay, Stage A refit, cutoff search, or entry-state threshold tuning.

The 2026-09-18 entry-state classification was sealed locally before the 2026-09-21 session; its independent seal is under ignored `runtime/seals/20260918.json`. The 2026-09-18 to 2026-09-21 outcome is deliberately absent. Stage A signals on 2026-09-09 through 09-15 are **RETROSPECTIVE_CAUSAL_RECONSTRUCTION**; only an existing, verified seal may be called **PROSPECTIVE_SEALED**. Entry-state results before 09-18 are **POST_HOC_EXPLORATORY_CLASSIFICATION** and cannot validate an entry filter.

Outcome price limits come from official TWSE all-securities TWT84U (`selectType=ALL`) for the outcome date and the official TPEx regular-session raw CSV's `次日漲停價` from the signal date. Both are cached and SHA-256 checked. Limit flags use official limit prices, not an arbitrary 9.9% return. The same-day eligible universe reuses Stage A's causal eligibility. Unavailable T+1 rows are not counted as misses; a missing Top30 T+1 bar fails closed.

The seven daily observations make this a **SHORT_SAMPLE** study. A high close-limit rate is not evidence of executable profit: open-at-limit, gap, open-to-high, open-to-close, and open-to-low are separately reported. Daily OHLC does not reveal first-hit time, order-book depth, or obtainable fills. Random Top30 permutation preserves each signal date's eligible universe and draws 30 unique stocks, with a fixed seed. Bootstrap resamples signal dates and, separately, selected stocks; the stock-cluster comparison holds the observed market rate fixed and is labelled accordingly.

The separate `stage_a_t1_outcomes_v01` module appends matured outcomes to its own hash-chained runtime ledger. It never mutates Stage A signal seals, and it cannot mature the 09-18 signal before 09-21 regular-session close and available official data. Notification is optional and deliberately not in the critical sealing path.

Run tests: `python3 -B -m unittest discover -s stage_a_t1_extreme_upside_study_v01/tests -v`.

After tests, publish the fixed historical sample once with:

`python3 -B -m stage_a_t1_extreme_upside_study_v01.main publish --active-inputs shadow_daily_runner/runtime/active_inputs.json --direct-official-audit shadow_daily_runner/runtime/audit/official_eod_through_20260918.json`

The CLI refuses to overwrite published artifacts. All input and output hashes are recorded in `run_manifest.json`.
