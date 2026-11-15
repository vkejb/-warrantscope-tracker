# V2 canary gate and exit diagnostic

Frozen before calculation on 2026-11-15. Research only: `SHADOW_ONLY`,
`NO_LIVE_ORDER`, `actual_orders=0`, `actual_fills=0`.

## Scope

- Prospective canary comparison uses the sealed 2026-11-12 and 2026-11-13
  realtime observer runs, not the later code's reconstruction of their choices.
- 11/12 is covered only through the archived 13:20 stop. 11/13 is covered only
  through the archived 10:18 stop. The latter is not a full trading day.
- Earlier 18-day V2 research is already inspected exploratory evidence. Its
  pre-3-minute strategy is a different version and is never merged into the
  canary A/B account.

## Entry comparison

- A: preserve the archived candidate selection and 180-second continuation
  result. A trade exists only for an archived mature candidate. Never derive a
  trade from a mere `SHADOW_SIGNAL_SELECTED` event.
- B: at each archived `CANDIDATE_SEEN`, require the existing V2 volume gate and
  the frozen +1.00 percentage-point relative-strength challenger. Select the
  earliest passing decision time, then highest production score and symbol.
  One board-lot position and at most one entry per session, NT$190,000 capital.
- Both arms share the same archived base candidate generation and quote period.
  The B fill proxy uses the first received tick at or after signal +1 second,
  within five seconds, its ask plus one legal tick, a fresh book within ten
  seconds, spread <=40 bps, and the existing fee-aware whole-lot sizing. This
  is not a confirmed queue fill. The archived five-level volume units and
  queue position are unverified; strict executable results remain NA.
- A five-second / two-tick adverse fill is a sensitivity scenario, not a
  measured broker latency distribution. No last-price fill is allowed.

## Exit and checkpoints

- Use the existing offline production-parity LONG exit simulator without
  changing stop, MFE trail, reversal, or 13:20 hard exit.
- Mark 5- and 15-minute net values using only the most recently received bid
  no older than three seconds at each checkpoint. Missing coverage is NA.
- Verify the frozen older 10-row V2 research gross/net totals independently.
- Paired breakeven diagnostic holds those same 10 entry times, prices and
  quantities. It arms after a positive observed bid-based after-cost net MFE.
  The floor is the first legal tick at which projected net PnL is >=0 using
  the existing fee/tax model. If a later observed bid is at or below the
  floor before the original exit, the proxy exit is that observed bid; a floor
  touch is not a broker fill guarantee. Otherwise retain original exit.

## Interpretation

All monetary outputs are conditional proxy fills using the existing research
fee schedule. No authorized brokerage statement proving account-specific
commission, minimum fee, rounding or partial fills is supplied. No result is
LIVE PnL. Previously viewed dates cannot be renamed untouched out-of-sample.
