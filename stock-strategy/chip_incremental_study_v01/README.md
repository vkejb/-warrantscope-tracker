# CHIP_INCREMENTAL_STUDY_V0_1

This independent research module tests whether official point-in-time chip data
adds path, downside, or payoff information inside the exact frozen Stage A
Top30 upside pool. It does not modify or refit Stage A, N_RETEST, N Compact, the
published OHLCV conditional model, the prospective ledger, or the daily runner.

## Point-in-time contract

- Official TWSE and TPEx institutional and margin/short daily tables are used.
- Historical rows are usable only from the next market session. A report date
  is not treated as proof of same-day public availability.
- The source date must be earlier than signal date T. A one-extra-session lag is
  reported as a timing sensitivity.
- Current snapshots never backfill history. Missing data remains missing.
- TDCC is rejected because the complete 2020–2025 official history and
  per-observation publication timestamps are unavailable. Securities lending
  and broker-branch families are not tested without a verified two-market PIT
  archive.

## Fixed model comparison

The primary population is the published Stage A Top30. The published OHLCV
conditional probability and ranking are loaded without refitting. The new
model is logistic ridge on the same frozen OHLCV inputs plus preregistered chip
features. C is selected only by the two discovery walk-forward folds from
`{0.01, 0.1, 1, 10}`. The final model is fit once on 2020–2022 and then applied
unchanged to 2023–2024 and 2025.

## Commands

```bash
python -m chip_incremental_study_v01.main audit-sources
python -m chip_incremental_study_v01.main download-official
python -m chip_incremental_study_v01.main publish
```

`download-official` is deliberately bounded and sequential. Its defaults fetch
at most 20 source/date payloads per invocation, wait five seconds between
successful requests, retry at most twice, and use a 30-second exponential
backoff. Options may make a batch smaller or slower, but should not be used to
turn it back into a high-concurrency downloader:

```bash
python -m chip_incremental_study_v01.main download-official \
  --max-source-requests 20 \
  --request-interval-seconds 5 \
  --max-attempts 2 \
  --initial-backoff-seconds 30
```

Every completed `source × date` is immediately stored as an immutable raw and
parsed pair under `runtime/official_cache/`. Each pair is committed by one
atomic directory rename, so a process interruption cannot expose a half-written
date as complete. The append-only
`download_manifest.jsonl` records request status, official and matched row
counts, retrieval timestamp, raw/parsed hashes, and retry count. A rerun checks
and skips completed cache pairs. CDN rejection or a bounded-batch stop writes
`download_progress.json` and exits without constructing the research store.
TWSE CDN responses such as a 307/308 without a usable data response are treated
as rate-limit rejection, not as a valid redirect or an empty market day.

The final `chip_daily_store.npz` is assembled only after all 1,459 formal
2020–2025 trading dates, plus the small required 2019 year-end feature warm-up,
have all four official payloads and pass the duplicate, parse, two-market
coverage, and PIT-lag gates. Warm-up rows are never included in formal
statistics. The official download and large observation stores remain local
under `runtime/`. Published CSV and JSON summaries are immutable: `publish`
refuses to overwrite them.

## Evidence labels

- 2020–2022: `HISTORICAL_DISCOVERY`
- 2023–2024: `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`
- 2025: `STRESS_PREVALENCE_SEEN_NOT_BLIND`
- 2026 prospective observations are excluded from fitting and selection.

This module contains no broker SDK, account access, order, or fill path.

## Current checkpoint

Phase 0 is complete at `checkpoints/phase0/`. The formal model study has not
run. Both official daily families passed the PIT audit only with a one-session
lag, but a complete 2020–2025 archive was not accepted because the first bulk
download attempt triggered the official CDN Anti-DDoS controls. Partial dates
were discarded. Resume only with a rate-safe official acquisition or an
authorized official bulk file; do not substitute third-party history.

The Phase 1 acquisition engineering checkpoint is recorded at
`checkpoints/phase1_acquisition/`. Its first bounded live attempt received a
HiNetCDN HTTP 307 with no usable market payload, so coverage remains zero and
the study remains unfit. The operational manifest and any future official raw
payloads stay local under the gitignored `runtime/official_cache/`.
