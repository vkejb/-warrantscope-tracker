#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
attempt_code=0
python3 -B -m shadow_daily_runner.main attempt || attempt_code=$?
python3 -B -m prospective_notifications_v01.main status
exit "$attempt_code"
