# Ownership / Crowding Data Availability and Point-in-Time Schema

Audit date: 2026-09-06 (Asia/Taipei)

This document records source availability and the minimum point-in-time (PIT)
contract for future ownership/crowding research. It does not add ownership or
margin features to the current study, and it does not authorize reconstructing
history from current snapshots.

## Current status

```text
OWNERSHIP_FEATURES = NOT_TESTED_DATA_UNAVAILABLE
TDCC_OWNERSHIP_FEATURES = NOT_TESTED_DATA_UNAVAILABLE
MARGIN_SHORT_FEATURES = AVAILABLE_OFFICIAL_NOT_INGESTED
```

The repository does not currently contain a reliable, timestamped 2020--2025
TDCC snapshot archive. The official TDCC interfaces currently exposed are not a
substitute for that missing archive. Official daily TWSE/TPEx margin and short
balance history appears sufficient for 2020--2025, but it is not ingested or
tested in this version and therefore must not affect any reported setup result.

## TDCC ownership availability audit

### Official sources

- [TDCC shareholder ownership dispersion query](https://www.tdcc.com.tw/portal/zh/smWeb/qryStock)
- [TDCC OpenAPI specification](https://openapi.tdcc.com.tw/tdcc-opendata-api-docs)
- [TDCC OpenAPI current dispersion snapshot (`1-5`)](https://openapi.tdcc.com.tw/v1/opendata/1-5)
- [Government Open Data listing](https://data.gov.tw/dataset/11452)
- [Official holding-tier example](https://data.gov.tw/api/front/file/download?uuid=50c4fb19-c6d0-4d88-8304-dfdf7338b732)

### Observation cutoff and retention

TDCC states that a dispersion observation is compiled from each depository
account's book-entry balance **after the close of business on the last business
day of the week**, after consolidation by holder ID. Therefore
`observation_end_date` is a weekly balance cutoff, not the timestamp at which
the public could first retrieve the result.

The same official page states that the historical file was established from
July 2008, but that the data retention period is one year. Establishment in
2008 does not mean that the complete history remains downloadable today. At the
audit date, the official date selector exposed 52 observations from 2025-09-12
through 2026-09-04. This is not enough to test 2020--2025.

The public OpenAPI `GET /v1/opendata/1-5` has no historical-date parameter in
the published specification and returned the current weekly snapshot at audit
time. Its documented fields are:

- `資料日期`
- `證券代號`
- `持股分級`
- `人數`
- `股數`
- `占集保庫存數比例%`

### Public-time limitation

Neither the weekly records nor the OpenAPI schema provides a per-observation
`published_at` timestamp. A `資料日期` value proves the statistical cutoff; it
does **not** prove that the record was known to the market on that date. It is
therefore not permissible to infer a historical release timestamp from the
cutoff date, page metadata, a current API response, or a third-party chart.

For prospective collection, the first locally recorded successful fetch time
may be used as a conservative `known_at`. For an old snapshot with no retained
release or fetch timestamp, `known_at` remains unknown and the observation is
ineligible for a strict PIT backtest.

### Raw TDCC schema

| Field | Type | Requirement |
|---|---|---|
| `source` | string | Constant such as `TDCC_1_5` |
| `security_id` | string | Preserve leading zeroes |
| `observation_end_date` | date | Official `資料日期`; weekly balance cutoff |
| `published_at` | timestamp nullable | Official release time only; never inferred |
| `fetched_at_utc` | timestamp | Time this exact payload was first retained |
| `level_code` | integer | Official `持股分級` |
| `holders` | integer | Official holder count |
| `shares` | integer | Official share/unit count |
| `custody_pct` | decimal | Official percent of TDCC custody inventory |
| `source_url` | string | Exact official resource URL |
| `payload_sha256` | string | Immutable raw-payload provenance |

The raw payload must be retained unchanged. Normalization should be a separate,
reproducible layer keyed by `(observation_end_date, security_id, level_code)`.

### Correct derived-field naming

The official tiers do not support the mathematically exact labels "400 lots or
more" and "1,000 lots or more":

- levels 12--15 cover **400,001 shares and above**;
- level 15 covers **1,000,001 shares and above**;
- level 1 covers **1--999 shares**.

Exactly 400,000 shares belongs to level 11 and exactly 1,000,000 shares belongs
to level 14, so a tier aggregate cannot isolate inclusive 400-lot or 1,000-lot
thresholds. Use these precise names instead:

| Derived field | Definition |
|---|---|
| `tdcc_pct_400001_plus` | Sum of `custody_pct` for levels 12--15 |
| `tdcc_pct_1000001_plus` | `custody_pct` for level 15 |
| `tdcc_pct_1_999` | `custody_pct` for level 1 |
| `tdcc_pct_400001_plus_wow` | Current minus prior available weekly observation |
| `tdcc_pct_1000001_plus_wow` | Current minus prior available weekly observation |
| `tdcc_pct_1_999_wow` | Current minus prior available weekly observation |
| `large_up_1000_up` | Both large-holder deltas are positive |
| `large_up_retail_down` | `tdcc_pct_400001_plus_wow > 0` and `tdcc_pct_1_999_wow < 0` |
| `ownership_concentration_proxy` | Explicitly versioned proxy based on tier shares |

`ownership_concentration_proxy` must never be presented as a true
beneficial-owner concentration measure. TDCC explains that margin/short-sale,
stock-loan collateral, derivatives and other special accounts are counted by
the shares registered in those accounts. A large custody account can therefore
represent an intermediary or special-purpose account rather than one economic
owner.

Any week affected by a split, capital reduction, issue, merger, symbol change,
or a discontinuous custody-inventory denominator must carry a
`corporate_action_or_denominator_break` flag. Week-over-week changes are null
until the continuity check passes.

### PIT as-of join contract

For a signal evaluated at `decision_timestamp`:

1. Define `known_at = published_at` only when an official historical release
   timestamp exists; otherwise use the first immutable `fetched_at_utc` from a
   prospective archive.
2. Select only rows where `known_at <= decision_timestamp`.
3. Within those rows, take the maximum `observation_end_date` per security.
4. Require the prior weekly observation to satisfy the same rule before
   computing a delta.
5. Do not forward-fill across a listing gap, identifier remap, corporate-action
   break, or unexplained denominator break.
6. If `known_at` is unknown, return missing. Do not fall back to the observation
   date.

This contract deliberately rejects current-data backfills, screenshots, and
third-party histories without timestamped raw official provenance.

## TWSE / TPEx margin and short balances

### Official sources and history

- [TWSE margin and short balance query](https://www.twse.com.tw/zh/trading/margin/mi-margn.html)
- [TWSE dated JSON endpoint template](https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20200102&selectType=ALL&response=json)
- [TPEx margin and short balance query](https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html)
- [TPEx dated JSON endpoint template](https://www.tpex.org.tw/www/zh-tw/margin/balance?date=2020/01/02&id=&response=json)

TWSE states that this information is available from 2001-01-01. TPEx states
that its current query covers January 2007 onward and links a legacy query for
August 2003 through December 2006. A dated 2020-01-02 response was available
from both official endpoints during this audit, so official daily history is in
principle long enough for the requested 2020--2025 window.

These APIs are daily, end-of-trading-day records. Historical responses identify
the trading date but do not provide a per-record publication timestamp. The
conservative historical rule is therefore:

```text
available_from(D) = next trading session after source trade date D
```

If a setup is evaluated at the close of signal date T, the strict fallback
allows only `source_trade_date <= previous_trading_day(T)`. A prospective
collector may use date T for a T+1 decision only when its immutable fetch log
proves that the complete official payload arrived before that decision. This
conservative lag must not be silently relaxed merely because an endpoint now
answers an old dated query.

### Raw margin/short schema

| Field | Type | Requirement |
|---|---|---|
| `source` | string | `TWSE_MI_MARGN` or `TPEX_MARGIN_BALANCE` |
| `market` | string | `TWSE` or `TPEX` |
| `security_id` | string | Preserve leading zeroes |
| `trade_date` | date | Official report date |
| `available_from` | date/timestamp | Conservative next-session rule or proven fetch time |
| `financing_buy_lots` | integer nullable | Daily financing purchases |
| `financing_sell_lots` | integer nullable | Daily financing sales |
| `financing_cash_repayment_lots` | integer nullable | Cash repayment |
| `financing_prev_balance_lots` | integer nullable | Previous balance |
| `financing_balance_lots` | integer nullable | Current balance |
| `short_sell_lots` | integer nullable | Daily short sales |
| `short_buy_lots` | integer nullable | Daily short buybacks |
| `short_stock_repayment_lots` | integer nullable | Stock repayment |
| `short_prev_balance_lots` | integer nullable | Previous balance |
| `short_balance_lots` | integer nullable | Current balance |
| `offset_lots` | integer nullable | Same-day financing/short offset |
| `eligibility_status_flags` | string | Preserve stop/eligibility/report flags |
| `fetched_at_utc` | timestamp | Immutable acquisition time |
| `source_url` | string | Exact dated official URL |
| `payload_sha256` | string | Immutable raw-payload provenance |

Candidate derived fields, after PIT validation, are:

```text
financing_delta = financing_balance_lots[D] - financing_balance_lots[D-1]
short_delta = short_balance_lots[D] - short_balance_lots[D-1]
short_margin_ratio = short_balance_lots[D] / financing_balance_lots[D]
```

`short_margin_ratio` is null when financing balance is zero. Zero values from a
security that is ineligible, suspended, newly listed, or absent from a report
must not be treated as an ordinary measured zero; retain and evaluate the
official status flags first.

## Plug-in interface and deferred work

Future ingestion should implement a provider boundary with no dependency from
setup detectors to source-specific response formats:

```text
OwnershipProvider.load_asof(security_ids, decision_timestamps)
MarginShortProvider.load_asof(security_ids, decision_timestamps)
```

Each provider must return normalized values plus `known_at`, source identity,
payload hash, availability status and validation flags. A provider returns
missing rather than choosing a later observation or guessing an availability
date.

Deferred tasks:

- build a prospective TDCC weekly raw-snapshot collector with immutable fetch
  timestamps and hashes;
- acquire an authorized, timestamp-verifiable official historical TDCC archive
  before attempting 2020--2025 ownership tests;
- add rate-limited TWSE and TPEx dated ingestion with raw-response caching;
- validate market/symbol history, trading-session alignment, units and status
  flags on both exchanges;
- add corporate-action and custody-denominator continuity checks;
- add fixture-only tests for tier aggregation, PIT joins, next-session lag,
  zero-denominator handling and fail-closed missingness;
- only then enable ownership/crowding diagnostics in a separately versioned
  study.

Until those gates pass, no TDCC ownership value, margin value, or derived
ownership/crowding feature may enter signal detection, ranking, outcome labels,
or performance attribution in `multi_setup_study_v01`.
