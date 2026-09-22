#!/bin/zsh

set -u
repo_dir="${0:A:h:h}"
vendor_dir="/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python"
cd "${repo_dir}"
export PYTHONPATH="${repo_dir}"
export YUANTA_SPARK_API_DIR="${vendor_dir}"
export DOTNET_ROOT="${vendor_dir}/.dotnet"
export DOTNET_ROOT_ARM64="${vendor_dir}/.dotnet"
"${vendor_dir}/.venv/bin/python" -m yuanta_intraday_shadow_v01.gui
