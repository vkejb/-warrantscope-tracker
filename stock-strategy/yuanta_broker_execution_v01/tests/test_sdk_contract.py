import os
from pathlib import Path
import unittest

from yuanta_broker_execution_v01 import load_api_types, validate_sdk_contract


class InstalledSdkContractTests(unittest.TestCase):
    def test_installed_sdk_contract_when_vendor_dir_is_configured(self):
        raw = os.environ.get("YUANTA_VENDOR_DIR", "").strip()
        if not raw:
            self.skipTest("YUANTA_VENDOR_DIR is not configured")
        vendor_dir = Path(raw).expanduser().resolve()
        result = validate_sdk_contract(load_api_types(vendor_dir))
        self.assertEqual(result["method_parameter_counts"]["SendStockOrder"], [3])
        self.assertIn("OrderQty", result["stock_order_properties"])


if __name__ == "__main__":
    unittest.main()
