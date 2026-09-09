#!/bin/zsh
set -euo pipefail

repo_plist="/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy/shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-daily.plist"
installed_plist="/Users/linyunyan/Library/LaunchAgents/com.linyunyan.warrantscope.shadow-daily.plist"
preflight_repo_plist="/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy/shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-preflight.plist"
preflight_installed_plist="/Users/linyunyan/Library/LaunchAgents/com.linyunyan.warrantscope.shadow-preflight.plist"
logs_dir="/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy/shadow_daily_runner/runtime/logs"
label="com.linyunyan.warrantscope.shadow-daily"
preflight_label="com.linyunyan.warrantscope.shadow-preflight"
user_id="$(/usr/bin/id -u)"

/usr/bin/plutil -lint "${repo_plist}"
/usr/bin/plutil -lint "${preflight_repo_plist}"
/bin/mkdir -p "${logs_dir}" "/Users/linyunyan/Library/LaunchAgents"
/usr/bin/install -m 0644 "${repo_plist}" "${installed_plist}"
/usr/bin/install -m 0644 "${preflight_repo_plist}" "${preflight_installed_plist}"

if /bin/launchctl print "gui/${user_id}/${label}" >/dev/null 2>&1; then
  /bin/launchctl bootout "gui/${user_id}/${label}"
fi
if /bin/launchctl print "gui/${user_id}/${preflight_label}" >/dev/null 2>&1; then
  /bin/launchctl bootout "gui/${user_id}/${preflight_label}"
fi
/bin/launchctl bootstrap "gui/${user_id}" "${installed_plist}"
/bin/launchctl bootstrap "gui/${user_id}" "${preflight_installed_plist}"
/bin/launchctl enable "gui/${user_id}/${label}"
/bin/launchctl enable "gui/${user_id}/${preflight_label}"
/bin/launchctl print "gui/${user_id}/${label}"
/bin/launchctl print "gui/${user_id}/${preflight_label}"
