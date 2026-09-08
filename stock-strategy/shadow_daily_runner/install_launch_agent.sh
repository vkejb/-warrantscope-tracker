#!/bin/zsh
set -euo pipefail

repo_plist="/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy/shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-daily.plist"
installed_plist="/Users/linyunyan/Library/LaunchAgents/com.linyunyan.warrantscope.shadow-daily.plist"
logs_dir="/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy/shadow_daily_runner/runtime/logs"
label="com.linyunyan.warrantscope.shadow-daily"
user_id="$(/usr/bin/id -u)"

/usr/bin/plutil -lint "${repo_plist}"
/bin/mkdir -p "${logs_dir}" "/Users/linyunyan/Library/LaunchAgents"
/usr/bin/install -m 0644 "${repo_plist}" "${installed_plist}"

if /bin/launchctl print "gui/${user_id}/${label}" >/dev/null 2>&1; then
  /bin/launchctl bootout "gui/${user_id}/${label}"
fi
/bin/launchctl bootstrap "gui/${user_id}" "${installed_plist}"
/bin/launchctl enable "gui/${user_id}/${label}"
/bin/launchctl print "gui/${user_id}/${label}"
