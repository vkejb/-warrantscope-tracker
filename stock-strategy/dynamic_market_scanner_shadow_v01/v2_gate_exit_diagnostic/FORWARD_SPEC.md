# Frozen A/B forward Shadow activation

Baseline: `7efafe0b2d7e9097030ad2bbc523de64cd80d6e0`.
First eligible new session: 2026-11-16, if it is a full trading day and the
existing 08:50 Yuanta collector completes. Earlier 20 replay dates are known
exploration and are never included in the forward cumulative series.

## Execution boundary

The installed collector already receives expanded-universe ticks/books and
0050 ticks/books. This study adds **no** broker login, subscription, order
adapter, or live order call. After the collector seals a full session and the
existing paper publications finish, `auto_runner` invokes a separate
post-close received-time replay. If that replay fails, the original collector
still reports its own result; `AB_FORWARD_FAILED` is explicit and no PnL is
published for the failed study day. `SHADOW_ONLY`, `NO_LIVE_ORDER`,
actual_orders=0, actual_fills=0, broker_order_calls=0.

Activation writes `runtime/ab_forward_v01/activation.json` once with HEAD,
source-file SHA-256, config hash, baseline and timestamp. Every future run
fails closed if code/config drift; changing version requires a new explicit
freeze, not silently extending this cohort. The complete source manifest and
artifact hashes are checked before calculation. No missing day is filled with
zero PnL.

## Fixed arms and records

- Both arms use the identical existing observer policy (volume acceleration,
  relative 0050 >= +1.00 percentage point **before** ranking and occupancy),
  same pre-open sealed pool, time ordering, NT$190,000 capital, fee-aware
  board-lot sizing, one selected candidate per day, and original LONG exit.
- B: signal-time selection, quote-proxy entry at signal+1 second. A: same
  selected candidate, 180-second continuation using original positive
  conservative net requirement, quote-proxy entry at due+1 second. No
  hindsight replacement if A rejects. BASE 1s/1 extra ask tick and STRESS
  5s/2 ticks are retained from `7efafe0`.
- All passing candidates, including later occupied-slot names, receive both
  signal and +180s snapshots. Each snapshot stores latest raw five buy/sell
  prices and volumes, tick bid/ask, tick exchange time if provided, received
  times, ages and freshness. Historical five-level callbacks have no exchange
  timestamp. Raw volume unit is UNKNOWN, capacity in shares UNKNOWN, queue
  fill unverified. Reasons for A absence are preserved separately from
  hypothetical delayed quote diagnostics.
- Each full day stores the original replay CSV/JSON, candidate checkpoint
  JSONL, activation snapshot, and `FORWARD_DAILY.json`: A/B counts, net PnL,
  matched entry price difference, no-trade reasons, cumulative total,
  excluding-best-day result, and B-minus-A decomposition into B extra trade
  versus same-symbol early entry **plus exit-path** difference. This is not a
  pure causal timing estimate.

## Full-day criterion and run location

`process_run` must mark `FULL_SESSION`; source manifest must be `COMPLETE`
with pre-open seal, required raw channels, SHA-256-valid artifacts and zero
callback errors. Forward results are published append-only under
`stock-strategy/yuanta_intraday_shadow_v01/runtime/ab_forward_v01/days/`.
`automatic_runs.jsonl` records `AB_FORWARD_COMPLETE`, `FAILED`, or
`UNAVAILABLE`. A weekday launch alone or an unscorable day is not a completed
forward observation. `STRICT_EXECUTABLE=NA` until book units, capacity,
queue, actual latency and account fees are confirmed.

Start/inspect (does not start a broker connection):

```sh
PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.forward_shadow activate
PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.v2_gate_exit_diagnostic.forward_shadow status
```

The already installed `com.linyunyan.warrantscope.yuanta-intraday-shadow`
LaunchAgent starts the existing collector at 08:50 on calendar weekdays.

## Pre-start dependency observed on 2026-11-15

The 2026-11-13 daily readiness audit is `READINESS_FAILED` with
`refusing calendar rebind because covered historical sessions changed`.
Consequently the 2026-11-13 Stage A seal is absent. The existing collector
requires the immediately preceding trading day's 30-stock seal, so its
2026-11-16 preflight will fail unless that **separate authoritative daily
workflow** produces a valid 2026-11-13 seal first. This A/B activation does
not repair the historical calendar, synthesize a seal, or fall back to the
2026-11-12 pool. An armed Shadow study is not proof of a collected forward
day; a failed/missing day stays missing rather than zero-PnL.
