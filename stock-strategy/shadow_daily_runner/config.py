from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    timezone: str = "Asia/Taipei"
    data_start: str = "20260102"
    direct_official_start: str = "20260831"
    prospective_start: str = "20260907"
    first_scheduled_target: str = "20260908"

    # These are external-runner attempt times, not signal thresholds.
    attempt_times: tuple[str, ...] = ("14:30", "15:00", "15:30", "16:00")
    earliest_attempt_time: str = "14:25"
    latest_attempt_time: str = "16:05"

    historical_release_last_week: int = 35
    historical_release_missing_weeks: tuple[int, ...] = (8,)
    minimum_market_coverage_ratio: float = 0.70

    twse_eod_url: str = (
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    )
    # Keep the same official TPEx non-fixed-price CSV source used by the
    # established weekly archive producer.  The broader dailyQuotes JSON has
    # different aggregate-volume semantics for no-price rows.
    tpex_eod_url: str = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
    twse_calendar_url: str = (
        "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
    )
    twse_news_url: str = "https://openapi.twse.com.tw/v1/news/newsList"
    release_base_url: str = (
        "https://github.com/yukishirotsubasa/tw-stock-data-release/"
        "releases/download/daily-close-csv"
    )
    release_project_url: str = (
        "https://github.com/yukishirotsubasa/tw-stock-data-release"
    )
    twse_eod_page_url: str = (
        "https://www.twse.com.tw/zh/trading/historical/mi-index.html"
    )
    tpex_eod_page_url: str = (
        "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/mi-pricing.html"
    )
    twse_calendar_page_url: str = (
        "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
    )

    runtime_dir: Path = MODULE_DIR / "runtime"
    shadow_store_dir: Path = STOCK_STRATEGY_DIR / "prospective_shadow_v01" / "data"

    @property
    def raw_dir(self) -> Path:
        return self.runtime_dir / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.runtime_dir / "clean"

    @property
    def audit_dir(self) -> Path:
        return self.runtime_dir / "audit"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def calendar_path(self) -> Path:
        return self.runtime_dir / "trading_calendar.csv"

    @property
    def calendar_metadata_path(self) -> Path:
        return self.runtime_dir / "trading_calendar.metadata.json"

    @property
    def active_inputs_path(self) -> Path:
        return self.runtime_dir / "active_inputs.json"

    @property
    def runner_state_path(self) -> Path:
        return self.runtime_dir / "runner_state.json"

    @property
    def attempt_log_path(self) -> Path:
        return self.runtime_dir / "attempts.jsonl"

    @property
    def historical_source_coverage_path(self) -> Path:
        return self.audit_dir / "historical_source_coverage.json"

    @property
    def historical_health_audit_path(self) -> Path:
        return self.audit_dir / "historical_health_audit.json"

    @property
    def historical_repair_active_inputs_path(self) -> Path:
        return self.runtime_dir / "historical_repair_active_inputs.json"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / ".shadow_daily_runner.lock"


CFG = RunnerConfig()
