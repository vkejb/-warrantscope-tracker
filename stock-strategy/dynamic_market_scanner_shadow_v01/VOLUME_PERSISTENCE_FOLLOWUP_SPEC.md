# EXPANDED_VOLUME_PERSISTENCE_FOLLOWUP_V1

This is a broker-free, long-only shadow study. It does not modify the production
strategy, scheduler, collector, notification path, or order adapter.

The candidate source remains the frozen current-engine first-approved signal per
symbol ledger. The study compares three pre-existing frozen volume gates and one
new bounded recovery rule:

- V1: the existing 120-second sustained, distributed buying gate.
- V2: the current 30-second recent-volume acceleration gate.
- V3: the existing V1 plus V2 conjunction.
- V4: V2 at the original signal; on failure, recheck the unchanged V2 rule at
  +30, +60, and +90 seconds. A recovered signal uses the then-current fresh ask
  plus one tick, must still hold at least the original entry-price breakout
  proxy, and must pass the existing opening/VWAP anti-chase limits, spread,
  TWD190 price cap, and whole-lot affordability checks.

BASE uses the original proxy entry for an original pass and the delayed causal
quote for a recovered pass. STRESS retains the existing five-second delay and
one additional adverse tick. All variants use the unchanged production exit
engine, TWD190,000 shared capital, and at most one trade per session.

The 16 original dates are known exploratory data. The 2026-11-11 outcome was
already viewed before this follow-up was frozen, so it is a post-outcome case,
not untouched prospective evidence. The module cannot evaluate an earlier
near-signal watcher because the frozen ledger begins only after the production
engine has approved a base signal; that hypothesis needs a separately frozen
near-signal definition and new-date collection.
