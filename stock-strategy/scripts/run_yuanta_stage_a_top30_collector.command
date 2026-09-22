#!/bin/zsh

set -u
repo_dir="${0:A:h:h}"
vendor_dir="/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python"

cd "${repo_dir}"
export PYTHONPATH="${repo_dir}"
export YUANTA_SPARK_API_DIR="${vendor_dir}"
export DOTNET_ROOT="${vendor_dir}/.dotnet"
export DOTNET_ROOT_ARM64="${vendor_dir}/.dotnet"

"${vendor_dir}/.venv/bin/python" -m yuanta_intraday_shadow_v01.collector_main --seconds 300
collector_status=$?

if [[ ${collector_status} -eq 0 ]]; then
  "${vendor_dir}/.venv/bin/python" -m yuanta_intraday_shadow_v01.analysis_main
  analysis_status=$?
else
  analysis_status=0
fi

print ""
if [[ ${collector_status} -eq 0 && ${analysis_status} -eq 0 ]]; then
  print "Stage A Top30 即時行情與盤中品質分析已完成 append-only 封存。"
elif [[ ${collector_status} -eq 0 ]]; then
  print "行情已封存，但盤中品質分析未完成（代碼 ${analysis_status}）。"
else
  print "Stage A Top30 即時行情收集未完成（代碼 ${collector_status}）。"
fi
read -r "?按 Enter 關閉視窗……"
if [[ ${collector_status} -ne 0 ]]; then
  exit ${collector_status}
fi
exit ${analysis_status}
