#!/bin/zsh

set -u
repo_dir="${0:A:h:h}"
vendor_dir="/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python"

cd "${repo_dir}"
export PYTHONPATH="${repo_dir}"
export YUANTA_SPARK_API_DIR="${vendor_dir}"
export DOTNET_ROOT="${vendor_dir}/.dotnet"
export DOTNET_ROOT_ARM64="${vendor_dir}/.dotnet"

"${vendor_dir}/.venv/bin/python" -m yuanta_intraday_shadow_v01.main --seconds 60
test_status=$?

print ""
if [[ ${test_status} -eq 0 ]]; then
  print "只讀串流測試已結束。"
else
  print "只讀串流測試未完成（代碼 ${test_status}）。"
fi
read -r "?按 Enter 關閉視窗……"
exit ${test_status}
