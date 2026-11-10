"""Frozen policy for the first strict expanded-universe replay."""

from __future__ import annotations

import hashlib

from yuanta_intraday_shadow_v01.collector import canonical_bytes


STRICT_POLICY = {
    "policy_id": "STRICT_MARKET_WIDE_V1",
    "frozen_at_taipei": "2026-11-10T00:00:00+08:00",
    "mode": "SHADOW_ONLY_POST_SESSION_CAUSAL_REPLAY",
    "direction": "LONG_ONLY",
    "source_engine": "PRODUCTION_LIVE_DIRECTION_ENGINE",
    "capital_twd": 190_000,
    "maximum_entry_price_twd": 190.0,
    "board_lot_shares": 1_000,
    "maximum_trades_per_session": 1,
    "maximum_positions": 1,
    "observation_seconds": 60,
    "minimum_confirmation_flow_delta": 0.0,
    "minimum_hypothetical_net_progress_twd": 0.0,
    "require_market_not_bearish_at_signal_and_confirmation": True,
    "require_breakout_held_on_executable_bid": True,
    "require_vwap_held_on_executable_bid": True,
    "recheck_production_anti_chase_limits": True,
    "candidate_deduplication": "FIRST_APPROVED_SIGNAL_PER_SYMBOL_PER_SESSION",
    "selection": (
        "EARLIEST_CONFIRMATION_CHECKPOINT_THEN_HIGHEST_"
        "HYPOTHETICAL_NET_PROGRESS_RELATIVE_STRENGTH_PRODUCTION_SCORE"
    ),
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}

STRICT_POLICY_HASH = hashlib.sha256(canonical_bytes(STRICT_POLICY)).hexdigest()
