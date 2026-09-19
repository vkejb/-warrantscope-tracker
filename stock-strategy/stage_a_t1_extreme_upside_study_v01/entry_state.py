from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Sequence

from surge_event_study_v01.models import Bar


RULES = {
    "schema_version": "1",
    "atr_sessions": 14,
    "ma5_sessions": 5,
    "ma20_sessions": 20,
    "ma20_slope_sessions": 5,
    "overheated": {"ret_1d_gte": 0.07, "ret_3d_gte": 0.12, "ret_5d_gte": 0.18},
    "ready": {
        "ret_1d_lte": 0.05,
        "ret_3d_lt": 0.08,
        "ret_5d_lt": 0.12,
        "ma5_atr_extension_min": -0.5,
        "ma5_atr_extension_max": 1.0,
        "ma20_atr_extension_min": 0.0,
        "ma20_atr_extension_max": 2.0,
    },
    "priority": ["OVERHEATED", "COOLING_BUT_WEAK", "READY", "WATCH"],
}


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class EntryState:
    signal_close: float
    atr14: float
    ret_1d: float
    ret_3d: float
    ret_5d: float
    ma5: float
    ma20: float
    ma20_5d_ago: float
    ma5_atr_extension: float
    ma20_atr_extension: float
    classification: str


def classify(bars: Sequence[Bar], signal_date: str) -> EntryState:
    if len(bars) < 25 or bars[-1].date != signal_date:
        raise ValueError("entry-state lookback is incomplete or extends past signal date")
    close = bars[-1].close
    ret_1d = close / bars[-2].close - 1
    ret_3d = close / bars[-4].close - 1
    ret_5d = close / bars[-6].close - 1
    ma5 = sum(bar.close for bar in bars[-5:]) / 5
    ma20 = sum(bar.close for bar in bars[-20:]) / 20
    ma20_5d_ago = sum(bar.close for bar in bars[-25:-5]) / 20
    trs = [
        max(
            bars[index].high - bars[index].low,
            abs(bars[index].high - bars[index - 1].close),
            abs(bars[index].low - bars[index - 1].close),
        )
        for index in range(len(bars) - 14, len(bars))
    ]
    atr14 = sum(trs) / 14
    if atr14 <= 0:
        raise ValueError("ATR14 must be positive")
    ma5_ext = (close - ma5) / atr14
    ma20_ext = (close - ma20) / atr14
    if ret_1d >= 0.07 or ret_3d >= 0.12 or ret_5d >= 0.18:
        state = "OVERHEATED"
    elif close < ma20 or ma20 <= ma20_5d_ago:
        state = "COOLING_BUT_WEAK"
    elif (
        ret_1d <= 0.05
        and ret_3d < 0.08
        and ret_5d < 0.12
        and -0.5 <= ma5_ext <= 1.0
        and 0 <= ma20_ext <= 2.0
        and close >= ma20
        and ma20 > ma20_5d_ago
    ):
        state = "READY"
    else:
        state = "WATCH"
    return EntryState(close, atr14, ret_1d, ret_3d, ret_5d, ma5, ma20,
                      ma20_5d_ago, ma5_ext, ma20_ext, state)
