# Prospective post-seal notifications

The existing `shadow_daily_runner` launchd attempt remains the daily scheduler.
On and after 2026-09-16, after the N scan has sealed, it uses the same ready
OHLCV inputs to seal the frozen Stage A Top30 and then sends one combined
Telegram summary. Existing macOS N notifications remain the local secondary
channel. Stage A or notification failure never rolls back the sealed N ledger.
An already-completed N target can retry Stage A without rerunning N.

Telegram credentials are read only from
`WARRANTSCOPE_TELEGRAM_BOT_TOKEN` and `WARRANTSCOPE_TELEGRAM_CHAT_ID`.
They are not stored in this repository, launchd plist, or the notification
ledger. The current user-level launchd job keeps calling the same runner; at
attempt startup a best-effort Keychain adapter loads both values into those
environment variables. A missing/locked Keychain never blocks sealing. Do not
put secrets in a committed plist or use `launchctl setenv` for the bot token.
Notification ledger records only digest/status/code.

On the user's own Mac Terminal, run
`python3 -B -m prospective_notifications_v01.main configure-keychain`.
It uses `/usr/bin/security add-generic-password ... -w` with `-w` last, so
the token is entered at a hidden Keychain prompt rather than passed in argv.
The setup verifies the bot with official `getMe`, checks webhook status, and
requires exactly one private chat in official `getUpdates`; it then displays
that chat ID locally and asks the user to enter it at a second Keychain
prompt. It refuses multiple recipients. No credential is ever requested in
Codex chat. A non-reversible Keychain verification marker binds token and chat
ID; a partial reconfiguration fails closed instead of messaging an old
recipient. Afterward run the `test` command below and confirm the Telegram
message on the phone. If no update exists, send a new message to the bot and
rerun setup; it safely updates the same Keychain item.

Run `python3 -B -m prospective_notifications_v01.main test` to test Telegram
and macOS without sealing, and `... status` for the latest N/Stage A seals and
notification state. `scripts/run_daily_prospective.sh` is a manual wrapper;
the installed daily launchd job can keep calling `shadow_daily_runner.main
attempt`. No system-level launchd installation is performed here.

If official data are missing, the existing readiness gate refuses N and Stage A
seals. No retrospective date override, broker connection, order, or fill exists.

The combined daily message preserves the complete Stage A rank order first and
then lists every frozen Entry State category underneath it.  The standalone
Entry State supplement uses the same rank-first, classification-second layout.
These categories use only the sealed price/ATR/moving-average contract and are
never rewritten when institutional or margin data arrive.  A separate
`send-chip-watch --date YYYYMMDD` command waits for all four official TWSE/TPEx
institutional and margin sources, keeps exact string security identities, and
then sends an explicitly unvalidated watchlist.  Its fixed list is the first
five Stage-A-ranked `OVERHEATED` names; chip values are annotations only because
the published chip study found no stable incremental next-day edge.  Missing or
partial official chip data sends no Telegram message and never affects sealing.
Checks run at 18:15, 19:15, 20:15, 21:15, and 22:15 Taipei time.  If the final
attempt is still incomplete, a single idempotent status message explains that
no watch candidates were produced, so silence cannot be confused with a failed
scheduler.
