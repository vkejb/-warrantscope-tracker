# CZSC Third Buy Incremental Study V0.1

This independent, research-only module evaluates the exact public CZSC daily
`cxt_third_buy_V230228` signal, both standalone and as a descriptive overlay on
the immutable Stage A Top30. It does not optimize CZSC, select a preferred bi
count, refit Stage A, change the common T+1-open outcome, or execute trades.

## Frozen contracts

- CZSC upstream: `waditu/czsc`, tag `0.9.27`, commit
  `2d676f987a93cbcc9067513753fe21dad8638c9f`.
- Signal: exact upstream `czsc/signals/cxt.py::cxt_third_buy_V230228`, `di=1`.
- Every output beginning `三买_XX笔` is included. Bi count is diagnostic only.
- The scanner updates one daily bar at a time and evaluates after T close. It
  resets only at the repository's frozen discontinuity segments and never reads
  T+1 bars to form a T signal.
- Returns and barriers reuse the existing common outcome beginning at T+1
  regular-session open, including the existing transaction-cost assumptions.
- Stage A is the published `LINEAR_RIDGE_MFE10` daily Top30, with zero refits.

## Evidence discipline

- 2020–2022: `HISTORICAL_DISCOVERY`
- 2023–2024: `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`
- 2025: `STRESS_PREVALENCE_SEEN_NOT_BLIND`

No later-period result changes the external signal, thresholds, bi counts, or
cohort definitions. Bootstrap inference clusters by signal date and calendar
month. Tail removal ranks positive winners by net return.

## Reproduction

Checkout the fixed upstream commit and install its pinned 0.9.27 requirements
in an isolated dependency directory, then run:

```bash
PYTHONPATH=. python3 -B -m czsc_third_buy_incremental_study_v01.main publish \
  --upstream-root /path/to/czsc-0.9.27 \
  --dependency-root /path/to/isolated-dependencies
```

Publication is fail-closed if any formal output already exists or an immutable
input/upstream hash differs. `runtime/scan_checkpoint.jsonl` is local,
resumable, and excluded from Git.

Safety invariant: `actual_orders = actual_fills = broker_connections = 0`.
