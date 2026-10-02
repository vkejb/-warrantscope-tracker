# Controlled profit-giveback A/R/U/RU replay

Research only. No broker login, order, process restart, LIVE change, or deployment occurred.

## Fixed definitions

- A: healthy original production stop/MFE/EOD exit path.
- R: freeze a causally confirmed prior high after its pullback; enter observation before touching it; exit only after current price and best bid weaken, buyer flow weakens, and executable net profit remains positive.
- Five-level persistent sell pressure is reported in parallel and is not required by the primary R rule.
- U: first actual trade at the verified official daily upper limit immediately creates one market-IOC sell intent for the remaining quantity.
- RU: earliest valid A, R, or U trigger wins. No duplicate sell is created.

## Four-version comparison

| Latency | Variant | Scorable/All | Net PnL | Matched vs A | vs A | R exits | Limit-touch cases | U exits | Partial | Giveback | Post-exit opportunity |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0ms | A | 11/11 | 498.0 | 11 | 0.0 | 0 | 1 | 0 | 0 | 25439.0 | 33919.0 |
| 0ms | R | 11/11 | -3194.0 | 11 | -3692.0 | 2 | 0 | 0 | 0 | 22049.0 | 38609.0 |
| 0ms | U | 11/11 | 1695.0 | 11 | 1197.0 | 0 | 1 | 1 | 0 | 23245.0 | 32722.0 |
| 0ms | RU | 11/11 | -3194.0 | 11 | -3692.0 | 2 | 0 | 0 | 0 | 22049.0 | 38609.0 |
| 250ms | A | 10/11 | 1184.0 | 10 | 0.0 | 0 | 1 | 0 | 0 | 21348.0 | 30826.0 |
| 250ms | R | 11/11 | -3393.0 | 10 | -4989.0 | 2 | 0 | 0 | 0 | 22248.0 | 38808.0 |
| 250ms | U | 10/11 | 1483.0 | 10 | 299.0 | 0 | 1 | 1 | 0 | 20850.0 | 30527.0 |
| 250ms | RU | 11/11 | -3393.0 | 10 | -4989.0 | 2 | 0 | 0 | 0 | 22248.0 | 38808.0 |
| 1000ms | A | 10/11 | 1183.0 | 10 | 0.0 | 0 | 1 | 0 | 0 | 21349.0 | 30827.0 |
| 1000ms | R | 11/11 | -2794.0 | 10 | -4389.0 | 2 | 0 | 0 | 0 | 21649.0 | 38209.0 |
| 1000ms | U | 10/11 | 2979.0 | 10 | 1796.0 | 0 | 1 | 1 | 0 | 19553.0 | 29031.0 |
| 1000ms | RU | 11/11 | -2794.0 | 10 | -4389.0 | 2 | 0 | 0 | 0 | 21649.0 | 38209.0 |

## Fixed-rule result

- At 250ms across 10 matched scorable trades, primary R changes net PnL from 1184.0 to -3805.0 TWD (-4989.0 vs A); it is not supported for deployment.
- Across 10 matched trades, U changes net PnL from 1184.0 to 1483.0 TWD (299.0 vs A), but only 1 trade triggered U, so this is not broad evidence.
- The conditional still-holding table is descriptive only and is excluded from these totals.

## Resistance filter impact (250ms)

### R_PRIMARY / PRICE_BID_ONLY

- R won the exit race on 2 / 11 trades.
- Total scorable net PnL: -3393.0 TWD across 11 trades.
- profitable_peak: 5
- prior_high_confirmed: 5
- departed_prior_high_zone: 5
- near_prior_high_observed: 5
- price_bid_failure: 3
- same_price_sell_pressure: 1
- bid_depth_weakening: 1
- buyer_flow_weakening: 2
- positive_executable_net: 2
- triggered: 2
- terminal reasons: NO_BUYER_FLOW_WEAKENING=1, NO_CAUSAL_PRICE_AND_BID_REJECTION=2, NO_PROFITABLE_HIGH=6, TRIGGERED=2

### R_PRIMARY / PRICE_BID_PLUS_FIVE_LEVEL

- R won the exit race on 1 / 11 trades.
- Total scorable net PnL: -4005.0 TWD across 10 trades.
- profitable_peak: 5
- prior_high_confirmed: 5
- departed_prior_high_zone: 5
- near_prior_high_observed: 5
- price_bid_failure: 3
- same_price_sell_pressure: 1
- bid_depth_weakening: 2
- buyer_flow_weakening: 2
- positive_executable_net: 1
- triggered: 1
- terminal reasons: NO_BUYER_FLOW_WEAKENING=1, NO_CAUSAL_PRICE_AND_BID_REJECTION=2, NO_FIVE_LEVEL_CONFIRMATION=1, NO_PROFITABLE_HIGH=6, TRIGGERED=1

### R_WIDER_ZONE_SENSITIVITY / PRICE_BID_ONLY

- R won the exit race on 3 / 11 trades.
- Total scorable net PnL: -2396.0 TWD across 11 trades.
- profitable_peak: 5
- prior_high_confirmed: 5
- departed_prior_high_zone: 5
- near_prior_high_observed: 5
- price_bid_failure: 4
- same_price_sell_pressure: 1
- bid_depth_weakening: 2
- buyer_flow_weakening: 3
- positive_executable_net: 3
- triggered: 3
- terminal reasons: NO_BUYER_FLOW_WEAKENING=1, NO_CAUSAL_PRICE_AND_BID_REJECTION=1, NO_PROFITABLE_HIGH=6, TRIGGERED=3

## Limit-up evidence

- Verified cases touching the official upper limit while the simulated position was still open: 1.
- A full-day high is never used as a substitute for the official limit price.
- A bid-only locked-limit book is retained as legal execution evidence; a missing ask is never fabricated.

## 250ms material trade impacts

| Date | Symbol | Variant | Winner | First observed | Observed px | Trigger | Trigger px | Fill | Fill px | Net PnL | vs A |
|---|---|---|---|---|---:|---|---:|---|---:|---:|---:|
| 20260924 | 3605 | R | R | 2026-09-24T09:50:40.222000+08:00 | 184.0 | 2026-09-24T09:51:04.359000+08:00 | 183.0 | 2026-09-24T09:51:05.023000+08:00 | 183.0 | 412.0 | - |
| 20261001 | 3094 | R | R | 2026-10-01T09:38:25.343000+08:00 | 64.7 | 2026-10-01T09:38:32.589000+08:00 | 64.7 | 2026-10-01T09:38:32.886000+08:00 | 64.5 | 185.0 | -4989.0 |
| 20261001 | 3094 | U | U | - | - | 2026-10-01T09:46:03.238000+08:00 | 67.7 | 2026-10-01T09:46:03.498000+08:00 | 67.15 | 5473.0 | 299.0 |

## Conditional still-holding cases (not part of A/R/U/RU PnL)

These rows deliberately ignore an earlier A exit only to answer what R would have done if the position still existed. They are not improvements to the complete strategy.

| Date | Symbol | Earlier A exit | Anchor | First observed | Observed px | R trigger | Trigger px | Fill | Fill px | Book confirmed |
|---|---|---|---:|---|---:|---|---:|---|---:|---|
| 20260923 | 3094 | 2026-09-23T09:57:24.327000+08:00 | 57.8 | 2026-09-23T09:59:10.934000+08:00 | 57.6 | 2026-09-23T09:59:15.004000+08:00 | 57.3 | 2026-09-23T09:59:15.545000+08:00 | 57.13333333333333 | False |
| 20260923 | 2221 | 2026-09-23T10:25:38.517000+08:00 | 168.5 | 2026-09-23T10:27:56.822000+08:00 | 167.5 | 2026-09-23T10:28:15.041000+08:00 | 166.5 | 2026-09-23T10:28:15.809000+08:00 | 166.0 | False |
| 20260924 | 3605 | 2026-09-24T11:45:44.505000+08:00 | 186.5 | 2026-09-24T10:46:40.067000+08:00 | 185.5 | 2026-09-24T11:46:14.728000+08:00 | 183.0 | 2026-09-24T11:46:15.510000+08:00 | 182.5 | False |
| 20260929 | 3605 | 2026-09-29T09:11:22.578000+08:00 | 184.0 | 2026-09-29T09:19:55.798000+08:00 | 183.0 | 2026-09-29T09:21:11.639000+08:00 | 183.0 | 2026-09-29T09:21:12.657000+08:00 | 182.5 | False |
| 20261001 | 3094 | 2026-10-01T10:15:48.206000+08:00 | 67.7 | 2026-10-01T10:15:06.415000+08:00 | 67.4 | 2026-10-01T10:15:48.206000+08:00 | 66.7 | 2026-10-01T10:15:48.716000+08:00 | 67.0 | False |
| 20261002 | 3094 | 2026-10-02T09:19:54.107000+08:00 | 73.0 | 2026-10-02T12:11:01.131000+08:00 | 72.7 | 2026-10-02T12:11:06.663000+08:00 | 72.4 | 2026-10-02T12:11:07.306000+08:00 | 72.2 | False |

## 2026-10-02 3094 reproduction

- A: MFE_PROFIT_PROTECTION at 2026-10-02T09:19:54.107000+08:00; fill 2000 shares at 71.6 on 2026-10-02T09:19:54.855000+08:00; net 2642.0 TWD; pre-exit MFE 4637.0 TWD; giveback 1995.0 TWD.
- R: MFE_PROFIT_PROTECTION at 2026-10-02T09:19:54.107000+08:00; fill 2000 shares at 71.6 on 2026-10-02T09:19:54.855000+08:00; net 2642.0 TWD; pre-exit MFE 4637.0 TWD; giveback 1995.0 TWD.
- U: MFE_PROFIT_PROTECTION at 2026-10-02T09:19:54.107000+08:00; fill 2000 shares at 71.6 on 2026-10-02T09:19:54.855000+08:00; net 2642.0 TWD; pre-exit MFE 4637.0 TWD; giveback 1995.0 TWD.
- RU: MFE_PROFIT_PROTECTION at 2026-10-02T09:19:54.107000+08:00; fill 2000 shares at 71.6 on 2026-10-02T09:19:54.855000+08:00; net 2642.0 TWD; pre-exit MFE 4637.0 TWD; giveback 1995.0 TWD.

## Unscorable outcomes

- 20260924 3605 at 250ms: STALE_BOOK_AFTER_ARRIVAL; remaining 1000 shares.
- 20260924 3605 at 1000ms: STALE_BOOK_AFTER_ARRIVAL; remaining 1000 shares.

## Interpretation boundaries

- Profit giveback measures MFE available before the actual simulated exit fill.
- Post-exit opportunity measures later counterfactual upside and is reported separately.
- NO_TRIGGER is retained as NO_TRIGGER; entries are never changed to manufacture a limit-up example.
- 3094 on 2026-10-02 is explicitly post-hoc and cannot validate generalization.

## Limitations

- The 20261002 3094 incident is post-hoc inspiration, not out-of-sample evidence.
- Only fixed entries with matching tick and five-level archives are included.
- Archived five-level timestamps are local receive times, not exchange book timestamps.
- Displayed depth is not guaranteed fill; replay uses only the first causal book after arrival.
- A partial IOC fill leaves the remainder explicit and the total trade PnL unscorable; no fill is invented.
- The archive-backed fixed-entry research path has no reversal flags, so A preserves stop/MFE/EOD but cannot score signal-reversal exits.
- No result changes live or production behavior.
