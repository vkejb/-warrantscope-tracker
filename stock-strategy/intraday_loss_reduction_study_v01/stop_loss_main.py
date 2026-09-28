from __future__ import annotations
import argparse
from pathlib import Path
from .stop_loss_analysis import build_stop_report, write_stop_report

MODULE_DIR = Path(__file__).resolve().parent

def _session(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path).resolve() for path in paths.split(",")]

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controlled stop-loss diagnostic")
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", type=Path, default=MODULE_DIR / "stop_loss_results")
    args = parser.parse_args(argv)
    report = build_stop_report(dict(args.session), args.capital)
    write_stop_report(report, args.output_dir.resolve())
    for row in report["summaries"]:
        print(f"{row['variant']}: net={row['net_pnl']:.0f} PF={row['profit_factor']:.3f} max_loss={row['maximum_loss']:.0f} DD={row['maximum_drawdown']:.0f} recovered={row['stopped_then_recovered_positive']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
