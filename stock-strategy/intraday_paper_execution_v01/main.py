"""Local command-line controls for the paper execution engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import PaperExecutionEngine, Side


def _prices(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("price must be SYMBOL=PRICE")
        symbol, price = value.split("=", 1)
        result[symbol.strip().upper()] = price.strip()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Persistent paper-order controls")
    result.add_argument("--db", type=Path, required=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status")

    submit = commands.add_parser("submit")
    submit.add_argument("--key", required=True)
    submit.add_argument("--symbol", required=True)
    submit.add_argument("--side", choices=[item.value for item in Side], required=True)
    submit.add_argument("--quantity", type=int, required=True)
    submit.add_argument("--price", required=True)
    submit.add_argument("--intent", choices=["ENTRY", "EXIT"], default="ENTRY")

    ack = commands.add_parser("acknowledge")
    ack.add_argument("--order-id", required=True)

    fill = commands.add_parser("fill")
    fill.add_argument("--order-id", required=True)
    fill.add_argument("--fill-id", required=True)
    fill.add_argument("--quantity", type=int, required=True)
    fill.add_argument("--price", required=True)

    cancel = commands.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)
    cancel.add_argument("--reason", required=True)

    confirm = commands.add_parser("confirm-cancel")
    confirm.add_argument("--order-id", required=True)

    stop = commands.add_parser("emergency-stop")
    stop.add_argument("--reason", required=True)

    flatten = commands.add_parser("flatten")
    flatten.add_argument("--price", action="append", default=[], required=True)

    reset = commands.add_parser("reset-stop")
    reset.add_argument("--reason", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    with PaperExecutionEngine(args.db) as engine:
        if args.command == "submit":
            engine.submit_order(
                idempotency_key=args.key,
                symbol=args.symbol,
                side=args.side,
                quantity=args.quantity,
                limit_price=args.price,
                intent=args.intent,
            )
        elif args.command == "acknowledge":
            engine.acknowledge(args.order_id)
        elif args.command == "fill":
            engine.record_fill(
                args.order_id,
                fill_id=args.fill_id,
                quantity=args.quantity,
                price=args.price,
            )
        elif args.command == "cancel":
            engine.request_cancel(args.order_id, args.reason)
        elif args.command == "confirm-cancel":
            engine.confirm_cancel(args.order_id)
        elif args.command == "emergency-stop":
            engine.emergency_stop(args.reason)
        elif args.command == "flatten":
            engine.force_flatten(_prices(args.price))
        elif args.command == "reset-stop":
            engine.reset_emergency_stop(args.reason)
        print(json.dumps(engine.snapshot(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
