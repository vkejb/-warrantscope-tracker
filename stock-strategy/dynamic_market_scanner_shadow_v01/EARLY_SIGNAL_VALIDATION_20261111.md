# 2026-11-11 early-signal offline validation

Source run: `20261111T005011.762860Z_d43f8694` (`COMPLETE`, callback errors 0).

The complete four quote streams were merged by received time and replayed
through the new observer. It processed 2,533,244 events in 81.5 seconds on the
development machine, with `VALID_SHADOW_EVIDENCE`, no observer errors,
`actual_orders=0`, and `actual_fills=0`. The cheap prefilter ran 491 times and
selected at most 37 names, below its operational cap of 40.

Diagnostic examples, not trading approvals:

| Symbol | PREWATCH | ARMED | early-ready observed | official candidate |
|---|---|---|---|---|
| 2313 | 09:40:30 | 09:40:30 | 09:41:23.273 | 09:40:30 |
| 2344 | 09:07:30 | 09:20:00 | 09:37:03.128 | 09:43:00 |
| 8150 | 10:23:00 | 10:23:00 | 11:31:05.152 | 11:41:30 |

The replay shows that the telemetry can expose earlier conditions, including
about 5 minutes 57 seconds for 2344 and about 10 minutes 25 seconds for 8150.
It does not establish that entering at those times is profitable, because the
early-ready observation deliberately precedes official confirmation,
buy-concentration, ranking, and continuation checks. New-date shadow collection
is required before any entry rule changes.
