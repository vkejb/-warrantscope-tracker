# Prospective V2 automation

## Operational boundary

After a new expanded-universe full-session paper replay succeeds, the existing
automatic shadow runner calls `publish_prospective_v2`. The publisher rejects
all session dates through 2026-11-09, verifies the sealed source artifacts,
rebuilds the current-engine full-day candidate ledger, and replays the frozen
V2 BASE and five-second delayed tracks.

The output is immutable per source/policy identity under the Git-ignored
`prospective_v2_runtime/days/YYYYMMDD/<run-id>/` directory. A top-level
`validation_status.json` is an aggregate view. Publisher failure is logged as
`PROSPECTIVE_V2_FAILED` but cannot fail the completed collector, expanded paper
result, dynamic diagnostic, or formal execution system.

The module imports no broker adapter. Every day manifest and aggregate assert:

- `actual_orders=0`
- `actual_fills=0`
- `broker_order_calls=0`
- `live_enabled=false`

## Frozen review

The aggregate scans dates in order. The first prefix where both BASE and STRESS
have at least ten scored trades across at least five distinct dates becomes the
only review prefix. Later dates are reported in current metrics but cannot
rewrite that first decision.

Both tracks must have positive net P&L, PF above one, and positive net P&L after
removing the best trade. A failure becomes
`REJECTED_AT_FROZEN_PROSPECTIVE_REVIEW`; a pass becomes only
`ELIGIBLE_FOR_SEPARATE_UAT_REVIEW_ONLY`. Neither state changes live settings.

## Current initialized state

As of 2026-11-10, no post-freeze full session has been published:

- status: `PENDING_MINIMUM_NEW_SAMPLE`
- prospective sessions: 0
- BASE/STRESS scored trades: 0 / 0
- live enabled: false

The state will advance only when the existing scheduled full-session collector
produces a new valid archive. No broker login, quote subscription, or new
LaunchAgent was added for this validation.

## Offline catch-up

For a full-session archive produced by a process that started before this hook
was deployed, run:

```bash
python -m dynamic_market_scanner_shadow_v01.prospective_v2_main \
  yuanta_intraday_shadow_v01/runtime/runs/<run-id>
```

The entry verifies and reuses the run's immutable
`session_analyses/<run-id>/session_manifest.json`. If it is absent, it creates
only that analysis; it does not repeat the automatic completion notification.
