# 2026-11-11 live activation review

Decision: `NO_GO_FOR_AUTOMATED_LIVE_2026-11-12`.

This is a read-only review. No broker login, reconciliation, order, service
restart, halt clearing, or production configuration change was performed.

## Current blocking evidence

- The installed shadow LaunchAgent starts this checkout at 08:50 on weekdays;
  the new early observer will therefore load on the next fresh process without
  changing the LaunchAgent.
- The installed trading-bot service uses code from this checkout but state from
  `/Users/linyunyan/Downloads/-warrantscope-tracker/stock-strategy/yuanta_live_runtime_v01/runtime`.
- Its authoritative `live-orders.sqlite` has `live_control.halted=1`, reason
  `RECONCILIATION_QUERY_FAILED`, updated 2026-11-05 13:20 Taipei time.
- The most recent formal events include externally adopted broker inventory and
  a broker-execution halt. There is no fresh 2026-11-11 broker reconciliation,
  position proof, order-path proof, or forced-flat proof.
- The supervisor process is alive, but its log repeatedly reports polling
  `RuntimeError`; process liveness is not execution readiness.
- The frozen V2 BASE replay remains negative after costs, and the apparent
  90-second recovery improvement is concentrated in the already-viewed
  2026-11-11 case. Manual supervision does not repair model uncertainty,
  reconciliation failure, stale broker state, or order/position races.

## Safe 2026-11-12 mode

- Keep automated orders disabled and the formal broker halt unchanged.
- Let the 08:50 shadow collector run normally.
- Observe `PREWATCH`, `ARMED`, official candidates, continuation, notification,
  dropped events, callback errors, and the terminal summary for the full day.
- Treat early states as telemetry only. They are not permission to enter.
- Reassess live activation only after fresh broker-backed reconciliation,
  zero unexplained inventory, verified order/fill/cancel/partial-fill handling,
  verified forced-flat behavior, and prospective strategy evidence.

Human intervention is a useful final safety layer, but it is not a substitute
for those controls. In particular, a person may react after a fast move, race
with an automated cancel or exit, or be unable to close a locked-limit or stale
quote position.
