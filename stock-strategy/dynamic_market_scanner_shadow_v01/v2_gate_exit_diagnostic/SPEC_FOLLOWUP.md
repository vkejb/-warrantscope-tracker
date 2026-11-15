# V2 candidate occupancy and fixed-hold follow-up

Frozen before follow-up PnL calculation on 2026-11-15. This is a separate
offline/Shadow study on a research branch; no installed service, formal LONG,
LIVE setting, broker adapter, or prior result is modified.

## Coverage

- Primary complete sessions with archived V2 volume candidate events: 2026-11-12
  `20261112T005011.064631Z_fcdaa5e4` and 2026-11-13
  `20261113T005010.446883Z_c876f743`, both archived 08:50–13:35 Taipei.
  The observer policy differed (V1 occupancy versus V2 rearm), so the
  upstream candidate-generation path is not proven identical across days;
  A/B within each day still receives exactly the same archived events.
- Context-only complete 2026-11-11 `20261111T005011.762860Z_d43f8694`
  used an earlier V1 observer policy, so is listed separately and not used to
  inflate the current-version evidence count.
- Later partial 11/12 and 11/13 runs are event cross-checks only, never
  duplicate trading days.

## Candidate and entry comparison

- Both A and B apply archived V2 recent-volume gate and `RS_0050_5m >= 0.01`
  before ranking/reservation. Equality passes. Earliest event time wins; ties
  rank by descending archived score, then symbol. One stock and one trade/day.
- A waits 180 seconds, requires a fresh causal quote and strictly positive
  after-cost conservative progress from original signal price/quantity, then
  enters. B enters immediately. Same 190,000 TWD, whole-board-lot affordability,
  original LONG exit engine, proxy costs, and BASE/STRESS delay/tick assumptions
  from `SPEC_FAIR_AB.md`.
- Never choose a different later candidate after the day's first eligible
  candidate fails A's continuation; this isolates entry timing from selection.

## Fixed holding and MFE

- From each arm's own proxy entry timestamp, independently value a hypothetical
  position at +5 and +15 minutes, even if the strategy already exited.
- Use the most recent received bid among a tick quote or five-level book at or
  before the target, with age <= 3 seconds. A stale/missing/nonpositive bid is
  `NA` with explicit reason. Value at bid with the same round-trip fee and tax
  estimator; no assumed fill or queue priority. This is **not** actual strategy
  PnL, and its possible post-exit price path has no impact on exit decisions.
- Original exit engine gives strategy exit time/reason/net and safe-exit-quote
  MFE/MAE during actual simulated holding. Derive 1R from its frozen initial
  stop price, not from a newly selected threshold; preserve its protection
  armed status and peak/floor telemetry.

## Output boundaries

- Record complete session count separately from current-version comparable
  count and from partial cross-checks. Include no-trade days in daily PnL.
- `STRICT_EXECUTABLE=NA`: five-level volume units and queue/partial fills are
  unverified, actual broker fees and order delay unknown. No actual orders.
- Freeze input hashes in a new manifest. Prior `20261115_fair_ab_v2` output is
  preserved, not overwritten.
