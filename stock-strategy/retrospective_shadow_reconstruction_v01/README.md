# Retrospective Shadow Reconstruction V0.1

This module reconstructs the missed 2026-09-07 and 2026-09-08 observations only.
Every result is labelled `RETROSPECTIVE_RECONSTRUCTION_YYYY-MM-DD` and
`RETROSPECTIVE_RECONSTRUCTION_NOT_PROSPECTIVE`. It must never be counted as a
prospective observation.

The module directly reuses the frozen `prospective_shadow_v01` provider and
detector. The detector's existing source/config hash contract remains in force:

```text
N_COMPACT_RETEST = pivot_separation_sessions <= 7 and bottom_difference > 0
```

It imports no `ShadowStore` and calls no prospective service. It requires and
only reads the four canonical prospective files to prove their hashes did not
change. The CLI offers no alternate-ledger argument that could bypass this
check. It has no broker,
account, order, fill, outcome-update, scan-log, or status-write path.

Run this only after the historical archive repair and full readiness audit pass:

```bash
cd stock-strategy
python3 -B -m retrospective_shadow_reconstruction_v01.main reconstruct \
  --signal-date 2026-09-07 \
  --signal-date 2026-09-08 \
  --archives /absolute/path/to/each/audited/archive.zip \
  --trading-calendar /absolute/path/to/trading_calendar.csv
```

Pass every active audited archive after `--archives`; the abbreviated example
shows only the argument shape. The command rejects all dates other than 9/7 and
9/8. Each result is written once under:

```text
output/RETROSPECTIVE_RECONSTRUCTION_YYYY-MM-DD/<content-derived-run-id>/
  signals.csv
  manifest.json
```

The run ID binds the date, frozen detector/config hashes, and the provider input
manifest hash. The manifest embeds archive/calendar provenance and hashes,
candidate counts, output hash, and before/after prospective-ledger hashes. A
same-input rerun verifies the immutable files; it cannot overwrite them.
