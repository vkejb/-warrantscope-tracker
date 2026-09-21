from pathlib import Path
import unittest

from yuanta_intraday_shadow_v01.main import _normalise_account, parse_args


class ReadOnlyContractTests(unittest.TestCase):
    def test_account_normalisation(self):
        self.assertEqual(_normalise_account("S1234-1234567"), "S12341234567")

    def test_cma_or_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            _normalise_account("1234567890")

    def test_duration_is_configurable(self):
        self.assertEqual(parse_args(["--seconds", "15"]).seconds, 15)

    def test_fixed_prod_stream_arguments(self):
        args = parse_args(["--prod", "--market", "twse", "--symbol", "2330"])
        self.assertTrue(args.prod)
        self.assertEqual(args.market, "twse")
        self.assertEqual(args.symbol, "2330")

    def test_source_has_no_order_api(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        forbidden = ("SendStockOrder", "SendFutureOrder", "StockOrder(", "FutureOrder(")
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
