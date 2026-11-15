# Frozen cohort replay: candidate continuation, ranking, waiting, exit

Frozen before calculating cohort performance. Research-only continuation of
`e795cc0`; no change to the deployed observer, LONG engine, LIVE configuration,
broker adapter, scheduler, or historical results.

## Source and replay

- Inspect the 18 dates in frozen `volume_persistence_followup_results/summary.json`
  plus complete 2026-11-12/13 archives. Each date must have a pre-open sealed
  expanded universe, full-session archived expanded ticks/books and 0050
  market-context ticks/books, valid manifest/artifact hashes, and zero callback
  errors. Partial duplicate runs are excluded.
- Re-run the **current** `RealtimeExpandedShadow` (policy hash
  `7f883490b65f2c74040fac43a50867401e8ee411d214c72726b5077cf31ca6f1`)
  from the archived received-time callback stream starting before 09:00.
  Observer events/summary are retained per day. Old prospective ledgers are
  only provenance, never substitutes for a current-version replay.
- The observer's `early_observation` pass is telemetry-only and is skipped
  during this cohort replay to reduce CPU; the unchanged `_engine.choose_entry`,
  V2 gate, RS, ranking, occupancy, and continuation logic still run on every
  archived callback. Verify the resulting `CANDIDATE_SEEN` identity/time/score
  against one full unoptimized replay before accepting the cohort output.
- All previously viewed dates are exploratory, not new out-of-sample evidence.
  A COMPLETE archive still may have stale 0050 or quotes at particular
  decisions; `NO_CANDIDATE` is qualified by the replay's data health.

## Candidate cohort

- Use every first-per-symbol `CANDIDATE_SEEN` with frozen V2 volume gate pass
  and `relative_strength_5m >= 0.01`; include later candidates and same-time
  score losers regardless of the observer's occupied slot. Do not pretend
  later signals were executable within one-trade/day account constraints.
- For each eligible candidate, quote-proxy entry at signal+1s with fresh ask
  plus 1 legal tick, and independently at signal+180s+1s; also 5s/two-tick
  stress. The delayed diagnostic is valued even when the A continuation gate
  fails, with gate result kept in a separate field.
- Fixed-hold net PnL at +1/+3/+5/+15 minutes from **each entry's** timestamp
  uses the freshest received bid (tick or book) at/before target, age<=3s,
  and frozen round-trip fee/tax model. Missing/stale quotes are NA, never zero.
- The first observed after-cost-positive bid and maximum after-cost bid quote
  in the first 15 minutes are descriptive quote paths, not guaranteed fills;
  record timestamps. Calculate same-decision-time score-rank comparisons only
  when at least two eligible candidates exist at that same timestamp.

## Account A/B and attribution

- Reuse `e795cc0` earliest eligible candidate selection and one trade/day.
  A waits 180s and requires strictly positive conservative after-cost progress
  from original signal; B enters immediately. Same capital, proxy fill, original
  LONG exit engine, costs, and BASE/STRESS conditions. Failed A continuation
  does not retrospectively select a different later candidate.
- Report A/B daily realized strategy proxy PnL separately from candidate
  fixed holds. On matched trades report entry price delta, 15m fixed-hold
  delta, actual strategy exit delta, and `max(0,MFE_net-final_net)` giveback.
  These are overlapping diagnostics, not additive causal components.
- `STRICT_EXECUTABLE=NA`: depth units, queue, partial fills, actual latency,
  account fees, and intraday eligibility remain unverified. No broker calls.
