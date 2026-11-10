# V2 expanded-universe prospective validation

Freeze time: 2026-11-10 Asia/Taipei.

Candidate: `EXPANDED_FROZEN_VOLUME_ACCELERATION_V2` with source config hash
`f1408450e0baadc89b8663d1b18d3a2cd343ea4dd3df690f7ba5d97099217280`.

All dates through 2026-11-09 are known exploratory data. A prospective date
must be a newly sealed full session generated after this freeze. Keep sessions
with no signal; data-insufficient and unfilled cases remain explicit and cannot
be recoded as no signal or zero P&L.

No promotion review occurs until there are at least:

- 10 scored V2 trades;
- 5 distinct trading dates with scored V2 trades.

At review, all of the following are required for both BASE and the frozen
five-second delayed/adverse-quote track:

- net P&L greater than zero;
- profit factor greater than one;
- net P&L after removing the best trade greater than zero;
- no formal orders, fills, or broker calls;
- complete shared-capital, one-trade-per-day replay and an explicit list of
  unfilled/unscorable selections.

Passing these gates only allows a separate UAT review. It does not enable live
orders. Failing a gate rejects the candidate; it does not authorize retuning
the 0.35 delta, 1.0 volume ratio, 30-second windows, or execution delay.
