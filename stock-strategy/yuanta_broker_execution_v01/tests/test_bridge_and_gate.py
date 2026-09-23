from dataclasses import dataclass
from enum import Enum
import unittest

from yuanta_broker_execution_v01 import (
    IntentBridgeError,
    IntentPurpose,
    LiveTradingGate,
    Side,
    StockOrderType,
    bridge_strategy_intent,
)


class StrategySide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyIntentType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class StrategyStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class StrategyIntent:
    intent_id: str = "intent-1"
    stock_id: str = "3605"
    side: StrategySide = StrategySide.BUY
    quantity_lots: float = 1.0
    suggested_limit_price: float = 171.5
    intent_type: StrategyIntentType = StrategyIntentType.ENTRY
    status: StrategyStatus = StrategyStatus.APPROVED


class GateTests(unittest.TestCase):
    def test_all_three_live_controls_are_required(self):
        cases = [
            ({}, False, False),
            ({"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"}, False, False),
            ({"EXECUTION_MODE": "DRY_RUN", "ENABLE_LIVE_TRADING": "YES"}, True, False),
            ({"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "NO"}, True, False),
            ({"EXECUTION_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"}, True, True),
        ]
        for environ, cli_live, expected in cases:
            with self.subTest(environ=environ, cli_live=cli_live):
                gate = LiveTradingGate.from_environment(
                    cli_live=cli_live, environ=environ
                )
                self.assertEqual(gate.authorized, expected)


class BridgeTests(unittest.TestCase):
    def test_long_entry_converts_lots_to_shares(self):
        result = bridge_strategy_intent(StrategyIntent())
        self.assertEqual(result.symbol, "3605")
        self.assertEqual(result.side, Side.BUY)
        self.assertEqual(result.quantity, 1000)
        self.assertEqual(result.purpose, IntentPurpose.ENTRY)
        self.assertEqual(result.order_type, StockOrderType.CASH)

    def test_long_exit_preserves_exit_purpose(self):
        result = bridge_strategy_intent(StrategyIntent(
            side=StrategySide.SELL,
            intent_type=StrategyIntentType.EXIT,
        ))
        self.assertEqual(result.side, Side.SELL)
        self.assertEqual(result.purpose, IntentPurpose.EXIT)
        self.assertEqual(result.order_type, StockOrderType.CASH)

    def test_rejected_intent_cannot_cross_bridge(self):
        with self.assertRaises(IntentBridgeError):
            bridge_strategy_intent(StrategyIntent(status=StrategyStatus.REJECTED))

    def test_sell_first_never_guesses_order_type(self):
        short = StrategyIntent(side=StrategySide.SELL)
        with self.assertRaises(IntentBridgeError):
            bridge_strategy_intent(short)
        result = bridge_strategy_intent(
            short, short_entry_order_type=StockOrderType.SHORT_SELL
        )
        self.assertEqual(result.order_type, StockOrderType.SHORT_SELL)

    def test_buy_to_cover_never_guesses_order_type(self):
        cover = StrategyIntent(
            side=StrategySide.BUY,
            intent_type=StrategyIntentType.EXIT,
        )
        with self.assertRaises(IntentBridgeError):
            bridge_strategy_intent(cover)
        result = bridge_strategy_intent(
            cover, short_cover_order_type=StockOrderType.SHORT_SELL
        )
        self.assertEqual(result.order_type, StockOrderType.SHORT_SELL)


if __name__ == "__main__":
    unittest.main()
