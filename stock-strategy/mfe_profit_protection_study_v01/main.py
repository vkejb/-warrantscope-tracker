from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import build_report, write_report


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = MODULE_DIR / "results"


def _session(value: str) -> tuple[str, list[Path]]:
    try:
        date, paths = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYYMMDD=RUN_DIR[,RUN_DIR]") from exc
    return date, [Path(path).resolve() for path in paths.split(",")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest-only controlled MFE profit-protection comparison"
    )
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument(
        "--trade-universe",
        choices=("live-parity", "independent-first-signals"),
        default="live-parity",
        help=(
            "live-parity selects the single executable entry; independent-first-signals "
            "replays each stock's first affordable signal as a non-portfolio diagnostic"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = build_report(
        dict(args.session),
        args.capital,
        trade_universe=args.trade_universe,
    )
    write_report(report, args.output_dir.resolve())
    for row in report["summaries"]:
        print(
            f"{row['variant']}: trades={row['total_trades']} "
            f"net={row['net_pnl']:.2f} mfe_exits={row['mfe_exits']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
