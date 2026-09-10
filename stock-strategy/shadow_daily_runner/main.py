from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .calendar import read_sessions
from .config import CFG
from .historical_repair import (
    load_previous_archives_for_preflight,
    public_repair_result,
    repair_history,
)
from .io_utils import process_lock
from .notifications import (
    safe_notify_unhandled_attempt_failure,
    send_test_notification,
)
from .pipeline import prepare_inputs, public_result, taipei_now
from .preflight import run_historical_preflight
from .runner import attempt, runner_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Official TWSE/TPEx EOD preparation and fail-closed external runner. "
            "No broker connection or order capability exists."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="prepare/audit today's data without sealing a scan")
    repair = commands.add_parser(
        "repair-history",
        help="official W01-W37 completeness audit and immutable patch creation",
    )
    repair.add_argument(
        "--through",
        required=True,
        help="inclusive data-repair date (YYYY-MM-DD); never a prospective signal override",
    )
    preflight = commands.add_parser(
        "preflight",
        help="audit already-prepared historical inputs without downloading or sealing",
    )
    preflight.add_argument("--through", required=True, help="inclusive YYYY-MM-DD")
    preflight.add_argument("--archives", nargs="+", required=True)
    preflight.add_argument(
        "--trading-calendar", default=str(CFG.calendar_path)
    )
    preflight.add_argument(
        "--source-coverage", default=str(CFG.historical_source_coverage_path)
    )
    commands.add_parser(
        "preflight-latest",
        help="scheduled local-only health check through the previous trading session",
    )
    commands.add_parser("attempt", help="scheduled current-day attempt; no date override exists")
    commands.add_parser("status", help="show runner and prospective-ledger counts")
    commands.add_parser(
        "test-notification",
        help="send one macOS notification without running or sealing the shadow pipeline",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            with process_lock(CFG.lock_path):
                prepared = prepare_inputs(cfg=CFG)
            result = public_result(prepared)
            code = 0 if prepared.ready or not prepared.trading_day else 3
        elif args.command == "repair-history":
            with process_lock(CFG.lock_path):
                repaired = repair_history(
                    args.through,
                    cfg=CFG,
                    progress=lambda value: print(value, file=sys.stderr, flush=True),
                )
            result = public_repair_result(repaired)
            code = 0
        elif args.command == "preflight":
            with process_lock(CFG.lock_path):
                health = run_historical_preflight(
                    archives=[Path(value) for value in args.archives],
                    calendar_path=Path(args.trading_calendar),
                    source_coverage_path=Path(args.source_coverage),
                    through_date=args.through,
                    cfg=CFG,
                )
            result = health.audit
            code = 0 if health.ready else 3
        elif args.command == "preflight-latest":
            local = taipei_now(cfg=CFG)
            today = local.date().isoformat()
            with process_lock(CFG.lock_path):
                sessions = read_sessions(CFG.calendar_path.read_bytes())
                prior_sessions = [
                    day
                    for day in sessions
                    if CFG.data_start <= day.replace("-", "") and day < today
                ]
                if not prior_sessions:
                    raise RuntimeError("no prior official trading session is available")
                through = prior_sessions[-1]
                archives = load_previous_archives_for_preflight(through, CFG)
                health = run_historical_preflight(
                    archives=archives,
                    calendar_path=CFG.calendar_path,
                    source_coverage_path=CFG.historical_source_coverage_path,
                    through_date=through,
                    cfg=CFG,
                    audit_path=(
                        CFG.audit_dir
                        / f"historical_preflight_scheduled_{today.replace('-', '')}.json"
                    ),
                )
            result = {
                **health.audit,
                "scheduled_local_date": today,
                "scheduled_local_time": local.isoformat(),
                "historical_through_date": through,
            }
            code = 0 if health.ready else 3
        elif args.command == "attempt":
            result = attempt(cfg=CFG)
            code = 0 if result["status"] in {
                "SUCCESS",
                "ALREADY_SUCCEEDED_NO_OP",
                "NON_TRADING_DAY_NO_OP",
            } else 3
        elif args.command == "status":
            result = runner_status(CFG)
            code = 0
        elif args.command == "test-notification":
            result = send_test_notification(CFG)
            code = 0 if result["status"] == "NOTIFICATION_TEST_SENT" else 3
        else:  # pragma: no cover
            raise AssertionError(args.command)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return code
    except Exception as exc:
        if args.command == "attempt":
            safe_notify_unhandled_attempt_failure(
                local=taipei_now(cfg=CFG),
                reason=exc,
                cfg=CFG,
            )
        print(
            f"shadow daily runner failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
