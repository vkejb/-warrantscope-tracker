# Yuanta SPARK read-only data audit

Audit date: 2026-09-27. Source: the locally installed official SDK
`IO_Doc/FunctionList.xlsx` and its matching I/O specifications. No broker login
or request was made during this audit.

| Source | Current use | Useful fields | Loss-control use | Priority |
|---|---|---|---|---|
| `SubscribeStockTick` | Used | trade, size, bid/ask, inside/outside flag, sequence, type | signed flow, staleness and serial integrity; `Type` still needs decoded logging | Existing |
| `SubscribeFiveTickA` | Used but collapsed | five prices and sizes each side | L1 pressure, weighted pressure, microprice, depth concentration/depletion | P0, use existing archive |
| `GetStockInformation` | Not used by strategy | `Dayoffmark`, `Lendremnants`, `LendSellMark`, `LendQty`, warnings | fail closed on ineligible day trade, sell-first suspension, no inventory, warnings | P0 |
| `GetWatchListAll` | Not used | prior/open reference, limits, OHLC, cumulative inside/outside volume, amount, order counts/qty | limit-lock, liquidity, market-flow confirmation and daily reference validation | P0 |
| `SubscribeWatchlistAll` | Not used | incremental best quote, cumulative inside/outside volume, total volume/amount, trading state | detect halt/call auction and flow regime changes | P1 |
| `SubscribeMarketInformation` | Not used; special permission | pre-open indicative trade, size, inside/outside bit, trading state | avoid abnormal opening auction and halt states | P1 after permission check |
| `SubscribeStockInformation` | Not used; special permission | indicative-auction trade time | auction timing integrity | P2 |
| `GetStkTickDetail` | Not used | same-day tick recovery with sequence | reconnect gap repair and completeness checks | P1 |
| `GetStkClassifyPrice` | Not used | price-level inside/outside volume | overhead supply/support diagnostics; not a standalone signal | P2 |
| `GetKLine` | Not used | 1/5/15/30/60-minute and daily OHLCV | warm-up history and regime context; never replace causal ticks for execution | P1 |

## Safe rollout

1. Archive the two P0 query results alongside the quote run with source time,
   schema version, hashes and callback errors.
2. Run the evidence gate in shadow mode and record every allow/reject reason.
3. Extract full-depth features from already archived books and validate them on
   new, complete sessions. Do not fit thresholds on the current three days.
4. Only after walk-forward evidence, promote one gate at a time to UAT. Production
   remains unchanged until a separate review.

## Non-negotiable data-quality gates

- Missing or stale eligibility/quote context rejects new shadow entries.
- Sequence gaps are recorded; `GetStkTickDetail` may repair analysis archives but
  must not invent the original callback ordering.
- Market state, limit locks and warning flags are evidence, not predictions.
- A positive theoretical SHORT replay is not executable until the broker reports
  sell-first eligibility and inventory for that symbol and day.
