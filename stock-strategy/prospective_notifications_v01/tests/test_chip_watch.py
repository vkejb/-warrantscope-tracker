from __future__ import annotations

import unittest
import json

from prospective_notifications_v01.chip_watch import _parse, select_observation_candidates


class ChipWatchTests(unittest.TestCase):
    def test_official_security_code_keeps_leading_zero_identity(self):
        fields = ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數"]
        raw = json.dumps({
            "stat": "OK", "date": "20260921", "fields": fields,
            "data": [["006203", "元大MSCI台灣", "1", "2", "3"], ["6203", "海韻電", "4", "5", "6"]],
        }, ensure_ascii=False).encode("utf-8")
        parsed = _parse("TWSE_INSTITUTIONAL", "20260921", raw, {"6203"}, "official")
        self.assertEqual([row["security_code"] for row in parsed["rows"]], ["6203"])

    def test_chip_never_changes_fixed_overheated_membership_or_rank(self):
        stage = {"stocks": [
            {"rank": index, "stock_id": str(1000 + index), "stock_name": f"S{index}"}
            for index in range(1, 31)
        ]}
        entry = {"stocks": [
            {"stock_id": str(1000 + index), "classification": "OVERHEATED" if index % 2 else "READY"}
            for index in range(1, 31)
        ]}
        chip = {
            str(1000 + index): {"foreign": -999999 if index == 1 else 999999, "investment_trust": 0, "dealer": 0}
            for index in range(1, 31)
        }
        selected = select_observation_candidates(stage, entry, chip)
        self.assertEqual([row["stage_a_rank"] for row in selected], [1, 3, 5, 7, 9])
        self.assertEqual(len(selected), 5)
        self.assertIn("賣超", selected[0]["chip_tags"][0])
        self.assertNotIn("-", selected[0]["chip_tags"][0])
        self.assertEqual(selected[1]["chip_tags"][0], "三大法人合計買超 999,999股")


if __name__ == "__main__":
    unittest.main()
