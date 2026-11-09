"""Frozen first-pass policy for the dynamic expanded-universe scanner."""

POLICY = {
    "policy_id": "DYNAMIC_EXPANDED_LONG_SHADOW_V1",
    "mode": "SHADOW_ONLY_POST_SESSION_CAUSAL_DIAGNOSTIC",
    "direction": "LONG_ONLY",
    "maximum_premarket_symbols": 400,
    "maximum_active_observations": 40,
    "maximum_entry_price_twd": 190.0,
    "capital_twd": 190_000,
    "board_lot_shares": 1_000,
    "commission_rate_each_side": 0.000855,
    "minimum_commission_twd": 20,
    "approved_gate_reasons": ["BASE_SIGNAL_CONFIRMED", "MARKET_GATE_PASSED"],
    "observing_gate_reasons": ["CONFIRMATIONS_INCOMPLETE"],
    "source_engine": "PRODUCTION_LIVE_DIRECTION_ENGINE_DIAGNOSTICS",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
