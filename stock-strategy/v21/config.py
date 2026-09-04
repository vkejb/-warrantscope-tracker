from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    start_date: str = "20220101"
    end_date: str = "20260828"
    max_holding_days: int = 8
    hard_stop_pct: float = 0.95
    trailing_activation_pct: float = 1.08
    trailing_drawdown_pct: float = 0.93
    min_price: float = 20.0
    max_price: float = 500.0
    min_avg_volume_lots: float = 2000.0
    min_daily_return: float = 0.02
    max_daily_return: float = 0.07
    min_volume_ratio: float = 1.8
    min_breakout_ratio: float = 0.98
    max_breakout_ratio: float = 1.05
    min_combined_ratio: float = 0.025
    max_entry_gap: float = 0.05
    commission_rate: float = 0.001425
    commission_discount: float = 0.28
    minimum_commission: float = 1.0
    stock_transaction_tax: float = 0.003
    per_trade_notional: float = 30_000.0
    slippage_scenarios: tuple[float, ...] = (0.0, 0.001)


CFG = Config()

# Historical industry membership is not present. This is an explicit, imperfect
# implementation aid, not a claim of complete point-in-time classification.
FINANCIAL_CODE_RANGES = ((2800, 2899),)
FINANCIAL_CODE_EXTRAS = {"5876", "5880"}
