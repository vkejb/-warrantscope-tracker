from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from conditional_path_quality_ranking_v01.data import protected_hashes
from sector_rotation_incremental_study_v01.data import load_frozen_arrays
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .config import CFG, Config


THIRD_BUY_VALUE = re.compile(r"^三买_(\d+)笔_任意_0$")


def scan_third_buy(
    prepared,
    meta: np.ndarray,
    upstream: dict,
    checkpoint: Path,
    checkpoint_identity: str,
    cfg: Config = CFG,
) -> tuple[np.ndarray, list[dict], dict]:
    """Causal sequential CZSC scan, reset at every frozen discontinuity segment."""

    expected_by_code: dict[str, set[int]] = {}
    for code in np.unique(meta["stock_code"]):
        code_text = str(int(code))
        expected_by_code[code_text] = set(
            int(value) for value in meta["signal_date"][meta["stock_code"] == code]
        )

    completed: dict[str, list[dict]] = {}
    if checkpoint.exists():
        with checkpoint.open(encoding="utf-8") as handle:
            header = json.loads(next(handle))
            if header != {"checkpoint_identity": checkpoint_identity, "schema_version": 1}:
                raise RuntimeError("CZSC scan checkpoint identity mismatch")
            for line in handle:
                row = json.loads(line)
                code = row["stock_id"]
                if code in completed:
                    raise RuntimeError("duplicate stock in CZSC scan checkpoint")
                completed[code] = row["signals"]
    else:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            json.dumps({"checkpoint_identity": checkpoint_identity, "schema_version": 1}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    CZSC, RawBar, Freq, signal_fn = (
        upstream["CZSC"], upstream["RawBar"], upstream["Freq"], upstream["signal"]
    )
    diagnostics = Counter()
    by_code = {stock.code: stock for stock in prepared}
    with checkpoint.open("a", encoding="utf-8") as handle:
        for code in sorted(expected_by_code, key=int):
            if code in completed:
                continue
            stock = by_code.get(code)
            if stock is None:
                raise RuntimeError(f"eligible stock is absent from prepared OHLCV: {code}")
            wanted_dates = expected_by_code[code]
            records: list[dict] = []
            analyzer = None
            prior_segment = None
            for bar_id, (bar, segment) in enumerate(zip(stock.bars, stock.segment_ids)):
                raw = RawBar(
                    symbol=code,
                    id=bar_id,
                    dt=datetime.strptime(bar.date, "%Y%m%d"),
                    freq=Freq.D,
                    open=float(bar.open), close=float(bar.close),
                    high=float(bar.high), low=float(bar.low),
                    vol=float(bar.volume), amount=float(bar.close * bar.volume),
                )
                if analyzer is None or segment != prior_segment:
                    analyzer = CZSC([raw])
                    diagnostics["segment_resets"] += 1
                else:
                    analyzer.update(raw)
                prior_segment = segment
                day = int(bar.date)
                if day not in wanted_dates:
                    continue
                output = signal_fn(analyzer, di=cfg.signal_di)
                if len(output) != 1:
                    raise RuntimeError("upstream Third Buy returned non-single signal")
                key, value = next(iter(output.items()))
                match = THIRD_BUY_VALUE.fullmatch(str(value))
                if match:
                    records.append({
                        "signal_date": day,
                        "stock_id": int(code),
                        "stock_name": stock.name,
                        "czsc_output_key": str(key),
                        "czsc_output": str(value),
                        "bi_count": int(match.group(1)),
                        "source_version": cfg.upstream_version,
                        "signal_definition_sha256": cfg.signal_definition_sha256,
                    })
            completed[code] = records
            handle.write(json.dumps({"stock_id": code, "signals": records}, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()

    signals = [row for code in sorted(completed, key=int) for row in completed[code]]
    signals.sort(key=lambda row: (row["signal_date"], row["stock_id"]))
    key_to_index = {
        (int(day), int(code)): index
        for index, (day, code) in enumerate(zip(meta["signal_date"], meta["stock_code"]))
    }
    mask = np.zeros(len(meta), dtype=bool)
    for row in signals:
        key = (row["signal_date"], row["stock_id"])
        index = key_to_index.get(key)
        if index is None:
            raise RuntimeError(f"CZSC signal escaped eligible mother sample: {key}")
        if mask[index]:
            raise RuntimeError(f"duplicate CZSC signal key: {key}")
        mask[index] = True
    return mask, signals, {
        "eligible_rows_scanned": len(meta),
        "prepared_stocks_scanned": len(completed),
        "third_buy_signals": len(signals),
        "unique_signal_dates": len({row["signal_date"] for row in signals}),
        "unique_signal_stocks": len({row["stock_id"] for row in signals}),
        "segment_resets": diagnostics["segment_resets"],
        "future_bars_used_for_signal": False,
        "same_day_close_execution_used": False,
        "discontinuity_segments_respected": True,
    }


def signal_store_digest(mask: np.ndarray, signals: list[dict]) -> str:
    digest = hashlib.sha256(np.ascontiguousarray(mask).tobytes())
    digest.update(json.dumps(signals, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


__all__ = [
    "load_frozen_arrays", "load_ohlcv", "prepare_stocks", "protected_hashes",
    "scan_third_buy", "sha256_file", "signal_store_digest",
]
