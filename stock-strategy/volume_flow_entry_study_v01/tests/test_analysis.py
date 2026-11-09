from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from volume_flow_entry_study_v01.analysis import (
    VARIANTS,
    evaluate_volume_gate,
    load_config,
    maximum_drawdown,
)


def ticks(
    end: datetime,
    *,
    previous_count: int = 10,
    recent_count: int = 12,
    positive: bool = True,
) -> dict:
    rows = []
    stamps = [
        end - timedelta(seconds=60) + timedelta(seconds=(index + 1) * 30 / (previous_count + 1))
        for index in range(previous_count)
    ] + [
        end - timedelta(seconds=30) + timedelta(seconds=(index + 1) * 30 / (recent_count + 1))
        for index in range(recent_count)
    ]
    for index, stamp in enumerate(stamps):
        flag = "1" if positive or index % 4 else "0"
        rows.append({
            "time": stamp,
            "received_at": stamp,
            "price": 100.0,
            "volume": 1.0,
            "bid": 99.9,
            "ask": 100.1,
            "flag": flag,
        })
    return {"ticks": rows, "books": []}


class VolumeGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.end = datetime(2026, 11, 9, 9, 30, tzinfo=timezone(timedelta(hours=8)))

    def test_baseline_always_passes_without_data(self) -> None:
        result = evaluate_volume_gate({"ticks": [], "books": []}, self.end, VARIANTS[0], self.config)
        self.assertTrue(result["passed"])

    def test_sustained_gate_passes_distributed_buying(self) -> None:
        result = evaluate_volume_gate(ticks(self.end), self.end, VARIANTS[1], self.config)
        self.assertTrue(result["evaluable"])
        self.assertTrue(result["passed"])

    def test_missing_window_is_not_zero_or_failure(self) -> None:
        result = evaluate_volume_gate(ticks(self.end, previous_count=2, recent_count=2), self.end, VARIANTS[1], self.config)
        self.assertFalse(result["evaluable"])
        self.assertEqual(result["reason"], "VOLUME_GATE_DATA_INSUFFICIENT")

    def test_acceleration_rejects_declining_volume(self) -> None:
        result = evaluate_volume_gate(ticks(self.end, previous_count=15, recent_count=5), self.end, VARIANTS[2], self.config)
        self.assertTrue(result["evaluable"])
        self.assertFalse(result["passed"])

    def test_future_tick_is_never_used(self) -> None:
        data = ticks(self.end, previous_count=2, recent_count=2)
        future = self.end + timedelta(seconds=1)
        data["ticks"].extend({
            "time": future,
            "received_at": future,
            "price": 100.0,
            "volume": 1000.0,
            "bid": 99.9,
            "ask": 100.1,
            "flag": "1",
        } for _ in range(50))
        result = evaluate_volume_gate(data, self.end, VARIANTS[1], self.config)
        self.assertFalse(result["evaluable"])

    def test_combined_requires_both(self) -> None:
        result = evaluate_volume_gate(ticks(self.end, previous_count=15, recent_count=5), self.end, VARIANTS[3], self.config)
        self.assertFalse(result["passed"])

    def test_drawdown_includes_later_losses(self) -> None:
        self.assertEqual(maximum_drawdown([100.0, -40.0, -80.0, 50.0]), 120.0)


if __name__ == "__main__":
    unittest.main()
