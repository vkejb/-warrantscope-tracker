from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from yuanta_intraday_shadow_v01.gui import duration_seconds


class GuiContractTests(unittest.TestCase):
    def test_fixed_duration_modes(self):
        zone = ZoneInfo("Asia/Taipei")
        now = datetime(2026, 9, 22, 9, 0, tzinfo=zone)
        self.assertEqual(duration_seconds("TEST", "30", now), 300)
        self.assertEqual(duration_seconds("MORNING", "30", now), 90 * 60)
        self.assertEqual(duration_seconds("FULL_DAY", "30", now), 275 * 60)

    def test_after_cutoff_fails_closed(self):
        zone = ZoneInfo("Asia/Taipei")
        with self.assertRaises(ValueError):
            duration_seconds("MORNING", "30", datetime(2026, 9, 22, 10, 31, tzinfo=zone))

    def test_gui_has_no_order_api(self):
        source = (Path(__file__).parents[1] / "gui.py").read_text()
        for forbidden in ("SendStockOrder", "SendFutureOrder", "StockOrder(", "FutureOrder("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
