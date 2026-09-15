# Frozen Stage A prospective Top30

Activation is 2026-09-16, with no prior Stage A prospective records. Current-day
only, during the existing 14:25–16:05 Taipei runner window, and only after
official OHLCV readiness has passed. T-day feature extraction uses the same
physically truncated data provider as N; same-day percentile preprocessing,
published `LINEAR_RIDGE_MFE10` coefficients, alpha 0.1, and deterministic Top30
sorting are reused without fit or parameter changes.

Daily seal files are created with exclusive-create semantics under ignored
`runtime/seals/YYYYMMDD.json`. They contain exactly 30 shadow-only names, ranks,
scores, model/config/input hashes, creation time, and content seal hash. A
rerun verifies the old seal and never edits it. There is no backfill command.
