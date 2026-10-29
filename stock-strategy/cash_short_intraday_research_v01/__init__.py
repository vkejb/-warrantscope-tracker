"""Offline cash-equity intraday SHORT research. No broker integration."""

RESEARCH_ID = "CASH_SHORT_INTRADAY_RESEARCH_V01"
SAFETY = {"mode": "SHADOW_ONLY", "live_orders_allowed": False,
          "actual_orders": 0, "broker_connections": 0, "market_subscriptions": 0}
