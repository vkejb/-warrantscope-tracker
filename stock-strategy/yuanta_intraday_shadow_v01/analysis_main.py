#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .analysis import publish_analysis
from .collector import DEFAULT_RUNTIME_DIR


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Stage A 盤中進場品質 Shadow 分析")
    parser.add_argument("--run-id", help="指定行情 run id；省略時使用最新 COMPLETE run")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    return parser.parse_args(argv)


def _source_run(runtime_dir: Path, run_id: str | None) -> Path:
    if run_id:
        path = runtime_dir / "runs" / run_id
        if not path.is_dir():
            raise RuntimeError(f"run not found: {run_id}")
        return path
    candidates = []
    for path in sorted((runtime_dir / "runs").glob("*"), reverse=True):
        manifest_path = path / "run_manifest.json"
        if manifest_path.is_file() and json.loads(manifest_path.read_text()).get("status") == "COMPLETE":
            candidates.append(path)
    if not candidates:
        raise RuntimeError("no COMPLETE quote run found")
    return candidates[0]


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        source = _source_run(args.runtime_dir.resolve(), args.run_id)
        output = publish_analysis(source, args.runtime_dir.resolve() / "analyses")
        manifest = json.loads((output / "analysis_manifest.json").read_text())
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        with (output / "intraday_features.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        print("\n盤中狀態（描述性、不是買賣訊號）：")
        for row in sorted(rows, key=lambda item: int(item["stage_a_rank"])):
            ret = float(row["first_to_last_return"]) * 100 if row["first_to_last_return"] else 0.0
            print(f"{int(row['stage_a_rank']):2d}. {row['stock_id']} {row['stock_name']}｜{row['state']}｜5分鐘 {ret:+.2f}%")
        return 0
    except FileExistsError:
        print("分析結果已存在；為維持 immutable，不覆寫。")
        return 3
    except Exception as exc:
        print(f"分析失敗：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
