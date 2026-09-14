from __future__ import annotations

from collections import defaultdict

from surge_event_study_v01.models import PreparedStock


TYPE_ORDER = {"LOW": 0, "HIGH": 1}


def raw_confirmed_pivots(stock: PreparedStock, left: int, right: int) -> list[dict]:
    """Return pivots only at their causal confirmation index."""

    rows: list[dict] = []
    bars = stock.bars
    for index in range(left, len(bars) - right):
        confirmation_index = index + right
        if stock.segment_ids[index - left] != stock.segment_ids[confirmation_index]:
            continue
        if stock.calendar_indices[confirmation_index] - stock.calendar_indices[index - left] != left + right:
            continue
        high = bars[index].high
        low = bars[index].low
        is_high = all(high > bars[index - offset].high for offset in range(1, left + 1)) and all(
            high >= bars[index + offset].high for offset in range(1, right + 1)
        )
        is_low = all(low < bars[index - offset].low for offset in range(1, left + 1)) and all(
            low <= bars[index + offset].low for offset in range(1, right + 1)
        )
        for pivot_type, active, price in (("LOW", is_low, low), ("HIGH", is_high, high)):
            if active:
                rows.append({
                    "stock_id": stock.code,
                    "stock_name": stock.name,
                    "pivot_type": pivot_type,
                    "pivot_index": index,
                    "pivot_date": bars[index].date,
                    "pivot_price": price,
                    "confirmation_index": confirmation_index,
                    "confirmation_date": bars[confirmation_index].date,
                    "left_sessions": left,
                    "right_sessions": right,
                })
    return sorted(rows, key=lambda row: (
        row["confirmation_index"], row["pivot_index"], TYPE_ORDER[row["pivot_type"]]
    ))


def compress_pivot(sequence: list[dict], pivot: dict) -> None:
    """Append causally, replacing only a more extreme consecutive same-type pivot."""

    if not sequence or sequence[-1]["pivot_type"] != pivot["pivot_type"]:
        sequence.append(pivot)
        return
    previous = sequence[-1]
    better = (
        pivot["pivot_price"] > previous["pivot_price"]
        if pivot["pivot_type"] == "HIGH"
        else pivot["pivot_price"] < previous["pivot_price"]
    )
    if better:
        sequence[-1] = pivot
    # Equal prices deliberately retain the earlier confirmed pivot.


def most_recent_abc(sequence: list[dict]) -> tuple[dict, dict, dict] | None:
    for index in range(len(sequence) - 3, -1, -1):
        triple = sequence[index : index + 3]
        if [row["pivot_type"] for row in triple] == ["LOW", "HIGH", "LOW"]:
            return triple[0], triple[1], triple[2]
    return None


def build_structure(abc: tuple[dict, dict, dict] | None, close: float, signal_index: int) -> dict:
    if abc is None:
        return {
            "structure_status": "MEASURED_MOVE_UNAVAILABLE",
            "unavailable_reason": "NO_ABC",
            "completion_ratio": None,
            "location_flag": None,
        }
    a, b, c = abc
    valid = (
        a["pivot_type"] == "LOW"
        and b["pivot_type"] == "HIGH"
        and c["pivot_type"] == "LOW"
        and a["pivot_index"] < b["pivot_index"] < c["pivot_index"]
        and b["pivot_price"] > a["pivot_price"]
        and c["pivot_price"] < b["pivot_price"]
        and c["pivot_price"] > a["pivot_price"]
        and b["pivot_index"] - a["pivot_index"] >= 1
        and c["pivot_index"] - b["pivot_index"] >= 1
        and max(a["confirmation_index"], b["confirmation_index"], c["confirmation_index"]) <= signal_index
    )
    length = b["pivot_price"] - a["pivot_price"]
    if not valid or length <= 0:
        return {
            "structure_status": "STRUCTURE_INVALID",
            "unavailable_reason": "STRUCTURAL_VALIDITY_FAILED",
            "completion_ratio": None,
            "location_flag": None,
            **_abc_fields(a, b, c),
        }
    target = c["pivot_price"] + length
    ratio = (close - c["pivot_price"]) / length
    return {
        "structure_status": "AVAILABLE",
        "unavailable_reason": "",
        **_abc_fields(a, b, c),
        "measured_move_length": length,
        "projected_target_d": target,
        "completion_ratio": ratio,
        "location_flag": "BELOW_C" if ratio < 0 else "AT_OR_ABOVE_C",
    }


def _abc_fields(a: dict, b: dict, c: dict) -> dict:
    result = {}
    for label, pivot in (("a", a), ("b", b), ("c", c)):
        result.update({
            f"{label}_pivot_date": pivot["pivot_date"],
            f"{label}_pivot_price": pivot["pivot_price"],
            f"{label}_confirmation_date": pivot["confirmation_date"],
            f"{label}_confirmation_index": pivot["confirmation_index"],
        })
    return result


def structures_by_signal_index(
    stock: PreparedStock, signal_indices: set[int], left: int, right: int
) -> tuple[dict[int, dict], list[dict]]:
    pivots = raw_confirmed_pivots(stock, left, right)
    by_confirmation: dict[int, list[dict]] = defaultdict(list)
    for pivot in pivots:
        by_confirmation[pivot["confirmation_index"]].append(pivot)
    sequence: list[dict] = []
    result = {}
    for index, bar in enumerate(stock.bars):
        for pivot in by_confirmation.get(index, []):
            compress_pivot(sequence, pivot)
        if index in signal_indices:
            result[index] = build_structure(most_recent_abc(sequence), bar.close, index)
    return result, pivots


def ratio_bucket(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio < 0:
        return "B0_R_LT_0"
    if ratio < 0.5:
        return "B1_R_0_TO_0_5"
    if ratio < 0.8:
        return "B2_R_0_5_TO_0_8"
    if ratio < 1.0:
        return "B3_R_0_8_TO_1_0"
    if ratio < 1.2:
        return "B4_R_1_0_TO_1_2"
    if ratio < 1.5:
        return "B5_R_1_2_TO_1_5"
    return "B6_R_GE_1_5"


def primary_group(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    if ratio < 0.8:
        return "A_R_LT_0_8"
    if ratio < 1.2:
        return "B_R_0_8_TO_1_2"
    return "C_R_GE_1_2"
