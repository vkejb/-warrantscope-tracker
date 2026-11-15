# 2026-11-13 daily-input / 2026-11-16 pre-open recovery

The installed 08:50 `com.linyunyan.warrantscope.yuanta-intraday-shadow`
LaunchAgent uses this checkout's `stock-strategy` directory and runtime. This
recovery did not start the broker collector or submit an order.

## Calendar cause

`readiness_20261113_original_failure.json` refused to rebind coverage because
the freshly fetched **undated** TWSE OpenAPI holiday endpoint had already
switched to ROC year 116 (2027). The old code accepted a nonempty schedule with
zero rows for requested 2026, producing weekday-only 2026 sessions except two
ad-hoc closure announcements. The existing coverage manifest spans
2026-01-02 through 2026-11-12 and has 207 sessions. The erroneous calendar
added these **16 dates inside** that same span; none are missing from the
manifest's official-session set:

`02-12, 02-13, 02-16, 02-17, 02-18, 02-19, 02-20, 02-27,
04-03, 04-06, 05-01, 06-19, 09-25, 09-28, 10-09, 10-26`.

The old coverage-bound calendar SHA-256 is
`035c4b15d6f8296dd2e7f3629ef9e6ededbc7e99c6b1c829a381911c28de37aa`.
An independent existing checkout retained byte-identical calendar content.
The archived 2026 official schedule SHA-256 is
`7644c1a8af784c09f54670fd7413f536b13eb76c54d658058e8873d1aee32117`.
It contains 27 schedule rows for ROC 115. Together with verified ad-hoc
closures, it rebuilds the bound calendar exactly and includes 11/13 and
11/16. The 2027 response SHA-256 is
`df6e0f8fac14030c1cd2d7fd49ee915f9365d49b9d833c588d121567d49c0281`.
The source client now rejects a fresh wrong-year response and may use only a
hash-checked, exact-request prior official snapshot for the requested year.
The existing coverage rebinding still rejects any changed historical session.

## Targeted EOD recovery

The full historical repair command encountered an unrelated cached official
source with unresolved invalid rows on 2026-09-17; it did **not** complete or
become the basis of this seal. The existing `prepare_inputs` path was run for
the missed 11/13 target under its process lock. It downloaded and validated
11/13 TWSE (1,081 rows) and TPEx (869 rows), produced the normal immutable
W46 official archive, extended source coverage to 208 sessions through
11/13, and passed all existing historical-preflight checks. The original
failure audit was preserved before that recovery.

`seal-preopen-prior` is explicitly limited to the immediately preceding
calendar session before 08:50. It used the audited 11/13 archives and exact
prospective input manifest hash, generated the normal frozen-model 30-stock
Stage A content, and wrote the actual creation time and recovery reason in
the immutable seal. It did not relabel a historical evaluation as a scheduled
11/13 run.

## Pre-open validation

With the installed LaunchAgent's executable and working directory,
`auto_runner._readiness(2026-11-16 08:45 Asia/Taipei)` returned
`('READY', '20261113')`. `latest_seal()` validated 30 stocks and seal hash
`bb947aa850a8b60f03bacdd84f54fcd240e7943653c537d802b7dce6ded157e3`.
The A/B config hash remains
`763d370692451ba3873d7599720a327ec562e1aed83333763052449dce266576`.
The freeze must be rechecked against the final committed HEAD before 08:50;
actual orders and fills remain zero in this recovery.

The 08:50 collector's actual market-data receipt and full-session status are
separate post-start observations. A failed/partial day is not a zero-PnL day.
