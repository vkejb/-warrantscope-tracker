# Prospective post-seal notifications

The existing `shadow_daily_runner` launchd attempt remains the daily scheduler.
On and after 2026-09-16, after the N scan has sealed, it uses the same ready
OHLCV inputs to seal the frozen Stage A Top30 and then sends one combined
Telegram summary. Existing macOS N notifications remain the local secondary
channel. Stage A or notification failure never rolls back the sealed N ledger.
An already-completed N target can retry Stage A without rerunning N.

Telegram credentials are read only from
`WARRANTSCOPE_TELEGRAM_BOT_TOKEN` and `WARRANTSCOPE_TELEGRAM_CHAT_ID`.
They are not stored in this repository or the notification ledger. A launchd
process does not automatically inherit shell-only environment settings; set
them privately for the launchd process if Telegram is desired. Missing
credentials yield `NOT_CONFIGURED` while sealing continues. Do not put secrets
in a committed plist. Notification ledger records only digest/status/code.

Run `python3 -B -m prospective_notifications_v01.main test` to test Telegram
and macOS without sealing, and `... status` for the latest N/Stage A seals and
notification state. `scripts/run_daily_prospective.sh` is a manual wrapper;
the installed daily launchd job can keep calling `shadow_daily_runner.main
attempt`. No system-level launchd installation is performed here.

If official data are missing, the existing readiness gate refuses N and Stage A
seals. No retrospective date override, broker connection, order, or fill exists.
