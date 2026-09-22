#!/bin/zsh

set -euo pipefail
repo_dir="${0:A:h:h}"
vendor_dir="/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python"
label="com.linyunyan.warrantscope.yuanta-intraday-shadow"
source_plist="${repo_dir}/yuanta_intraday_shadow_v01/${label}.plist"
target_plist="${HOME}/Library/LaunchAgents/${label}.plist"

cd "${repo_dir}"
export PYTHONPATH="${repo_dir}"
export YUANTA_SPARK_API_DIR="${vendor_dir}"

print "WarrantScope 元大只讀行情自動化設定"
print "帳密只會存入你的 macOS Keychain；請勿把內容貼到聊天。"
print ""
"${vendor_dir}/.venv/bin/python" -B -m yuanta_intraday_shadow_v01.yuanta_keychain configure

plutil -lint "${source_plist}"
mkdir -p "${HOME}/Library/LaunchAgents"
launchctl bootout "gui/${UID}/${label}" 2>/dev/null || true
cp "${source_plist}" "${target_plist}"
chmod 600 "${target_plist}"
launchctl bootstrap "gui/${UID}" "${target_plist}"
launchctl enable "gui/${UID}/${label}"

print ""
print "接下來設定週一至週五 08:45 自動喚醒，需要輸入一次 Mac 管理員密碼。"
sudo pmset repeat wakeorpoweron MTWRF 08:45:00

print ""
print "安裝完成："
launchctl print "gui/${UID}/${label}" | sed -n '1,35p'
pmset -g sched
print ""
print "交易日 08:50 自動登入只讀行情；13:35 封存、分析並通知。"
print "Mac 必須接上電源、處於睡眠而非關機，且早上有網路。"
read -r "?按 Enter 關閉視窗……"
