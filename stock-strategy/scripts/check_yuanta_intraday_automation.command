#!/bin/zsh

set -u
repo_dir="${0:A:h:h}"
vendor_dir="/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python"
label="com.linyunyan.warrantscope.yuanta-intraday-shadow"
cd "${repo_dir}"
export PYTHONPATH="${repo_dir}"
"${vendor_dir}/.venv/bin/python" -B -m yuanta_intraday_shadow_v01.yuanta_keychain status
print ""
launchctl print "gui/${UID}/${label}" 2>&1 | sed -n '1,45p'
print ""
pmset -g sched
read -r "?按 Enter 關閉視窗……"
