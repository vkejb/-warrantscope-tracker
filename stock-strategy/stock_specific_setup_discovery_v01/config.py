from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


PERIODS = (
    ("HISTORICAL_DISCOVERY", "20200101", "20221231"),
    ("RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS", "20230101", "20241231"),
    ("STRESS_PREVALENCE_SEEN_NOT_BLIND", "20250101", "20251231"),
)

SETUP_DEFINITIONS = (
    ("A1", "BREAKOUT", "Close_T > maximum Close over T-20 through T-1"),
    ("A2", "BREAKOUT", "A1 and Volume_T / median Volume over T-20 through T-1 >= 1.5"),
    ("A3", "BREAKOUT", "Close_T > maximum Close over T-60 through T-1"),
    ("A4", "BREAKOUT", "A3 and Volume_T / median Volume over T-20 through T-1 >= 1.5"),
    ("B1", "TREND_PULLBACK", "MA20_T > MA60_T; MA20_T > MA20_T-5; abs(Close_T-MA20_T) <= 0.50 ATR20_T; Close_T >= MA20_T"),
    ("B2", "TREND_PULLBACK", "MA20_T > MA60_T; MA20_T > MA20_T-5; Low_T <= MA20_T; Close_T >= MA20_T"),
    ("B3", "TREND_PULLBACK", "MA20_T > MA60_T; MA20_T > MA20_T-5; Close_T in [MA20_T-0.50 ATR20_T, MA20_T+0.25 ATR20_T]; Close_T > Close_T-1"),
    ("C1", "MEAN_REVERSION", "Close_T <= MA20_T - 1.0 ATR20_T"),
    ("C2", "MEAN_REVERSION", "Close_T <= MA20_T - 1.5 ATR20_T"),
    ("C3", "MEAN_REVERSION", "Close_T <= MA20_T - 1.0 ATR20_T and Close_T > MA60_T"),
    ("D1", "VOLATILITY_CONTRACTION_EXPANSION", "ATR5/ATR20 through T-1 <= 0.75 and Close_T > prior 10-session Close high"),
    ("D2", "VOLATILITY_CONTRACTION_EXPANSION", "ATR5/ATR20 through T-1 <= 0.85 and Close_T > prior 20-session Close high"),
    ("D3", "VOLATILITY_CONTRACTION_EXPANSION", "prior 5-session mean normalized range < prior 20-session mean normalized range * 0.70 and Close_T > prior 10-session Close high"),
    ("E1", "GAP_BEHAVIOR", "Gap_T >= 2%; Close_T > Open_T; close location >= 0.70"),
    ("E2", "GAP_BEHAVIOR", "Gap_T >= 2%; Close_T < Open_T; Close_T > Close_T-1"),
    ("E3", "GAP_BEHAVIOR", "Gap_T <= -2%; Close_T > Open_T; Close_T > Close_T-1"),
)


@dataclass(frozen=True)
class Config:
    study_id: str = "STOCK_SPECIFIC_SETUP_DISCOVERY_V0_1"
    execution_mode: str = "RESEARCH_ONLY_NO_EXECUTION"
    maximum_input_date: str = "20251231"
    minimum_history_sessions: int = 65
    primary_horizon_sessions: int = 5
    secondary_horizon_sessions: int = 10
    minimum_discovery_trades: int = 30
    minimum_discovery_day5_net_pf: float = 1.05
    minimum_positive_discovery_years: int = 2
    top5_removed_minimum_net_pf: float = 0.90
    maximum_positive_quarter_share: float = 0.75
    bootstrap_iterations: int = 5_000
    bootstrap_seed: int = 20_260_914
    commission_rate: float = 0.001425 * 0.28
    minimum_commission: int = 1
    sell_tax_rate: float = 0.003
    slippage_one_way: float = 0.001
    per_trade_notional: float = 30_000.0
    actual_orders: int = 0
    actual_fills: int = 0
    broker_connections: int = 0
    stage_a_refit_count: int = 0

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["periods"] = [
            {"label": label, "start": start, "end": end}
            for label, start, end in PERIODS
        ]
        payload["setup_definitions"] = [
            {"setup_id": setup_id, "family": family, "definition": definition}
            for setup_id, family, definition in SETUP_DEFINITIONS
        ]
        payload["multiple_testing_status"] = "MULTIPLE_HYPOTHESIS_RESEARCH"
        payload["warrant_research"] = False
        return payload

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


CFG = Config()
