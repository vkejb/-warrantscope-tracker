from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.setup_detectors import is_compact_retest
from reversal_event_study_v01.config import CFG as REVERSAL_CFG
from reversal_event_study_v01.study import build_pattern_observation
from surge_event_study_v01.models import PreparedStock

from .config import CFG, Config
from .market_data_provider import MarketDataSnapshot, normalize_date


@dataclass(frozen=True, slots=True)
class ScanResult:
    signal_date: str
    signals: tuple[dict, ...]
    raw_n_retest_count: int
    accepted_n_retest_count: int
    compact_count: int
    stocks_with_target_bar: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {
        "reversal_study_sha256": _sha256(
            root / "reversal_event_study_v01" / "study.py"
        ),
        "compact_detector_sha256": _sha256(
            root / "multi_setup_study_v01" / "setup_detectors.py"
        ),
    }


def assert_frozen_contract(cfg: Config = CFG) -> None:
    """Fail closed if either reused research contract has drifted."""

    if REVERSAL_CFG.fingerprint() != cfg.expected_reversal_config_hash:
        raise RuntimeError("frozen reversal config drifted")
    if MULTI_CFG.fingerprint() != cfg.expected_multi_setup_config_hash:
        raise RuntimeError("frozen multi-setup config drifted")
    hashes = source_hashes()
    if hashes["reversal_study_sha256"] != cfg.expected_reversal_study_hash:
        raise RuntimeError("frozen reversal detector source drifted")
    if hashes["compact_detector_sha256"] != cfg.expected_compact_detector_hash:
        raise RuntimeError("frozen compact detector source drifted")
    if REVERSAL_CFG.causal_cooldown_sessions != MULTI_CFG.causal_cooldown_sessions:
        raise RuntimeError("N_RETEST cooldown contracts disagree")
    probes = (
        ({"pivot_separation_sessions": 7, "bottom_difference": 1e-12}, True),
        ({"pivot_separation_sessions": 8, "bottom_difference": 1e-12}, False),
        ({"pivot_separation_sessions": 7, "bottom_difference": 0.0}, False),
    )
    for geometry, expected in probes:
        if is_compact_retest(geometry, MULTI_CFG) is not expected:
            raise RuntimeError("frozen N Compact rule drifted")


def _signal_row(
    stock: PreparedStock,
    local_index: int,
    legacy,
    snapshot: MarketDataSnapshot,
    cfg: Config,
) -> dict:
    geometry = legacy.geometry
    features = legacy.features
    first_index = int(geometry["first_pivot_index"])
    second_index = int(geometry["pivot_index"])
    hashes = source_hashes()
    return {
        "schema_version": cfg.schema_version,
        "signal_key": f"{legacy.code}|{legacy.signal_date}|{cfg.setup}",
        "stock_id": legacy.code,
        "stock_name": legacy.name,
        "signal_date": legacy.signal_date,
        "setup": cfg.setup,
        "parent_setup": cfg.parent_setup,
        "signal_close": legacy.signal_close,
        "first_pivot_date": str(geometry["first_pivot_date"]),
        "first_pivot_price": stock.bars[first_index].close,
        "first_pivot_close": stock.bars[first_index].close,
        "second_pivot_date": str(geometry["pivot_date"]),
        "second_pivot_price": stock.bars[second_index].close,
        "second_pivot_close": stock.bars[second_index].close,
        "pivot_separation_sessions": int(
            geometry["pivot_separation_sessions"]
        ),
        "bottom_difference": float(geometry["bottom_difference"]),
        "intervening_bounce": float(geometry["intervening_bounce"]),
        "confirmation_rebound": float(geometry["confirmation_rebound"]),
        "confirmation_break_vs_prior_high": float(
            geometry["confirmation_break_vs_prior_high"]
        ),
        "confirmation_close_location": float(
            geometry["confirmation_close_location"]
        ),
        "first_pivot_drawdown_20": float(
            geometry["first_pivot_drawdown_20"]
        ),
        "signal_return_1": float(features["signal_return_1"]),
        "signal_tr_vs_prior5": float(features["signal_tr_vs_prior5"]),
        "post_vs_pre_pivot_range": float(
            features["post_vs_pre_pivot_range"]
        ),
        "close_vs_sma20": float(features["close_vs_sma20"]),
        "pivot_drawdown_20": float(features["pivot_drawdown_20"]),
        "pivot_return_5": float(features["pivot_return_5"]),
        "pivot_atr5_vs_atr20": float(features["pivot_atr5_vs_atr20"]),
        "signal_lower_wick_fraction": float(
            features["signal_lower_wick_fraction"]
        ),
        "rs_5_vs_0050": float(features["rs_5_vs_0050"]),
        "pivot_volume_ratio_20": float(features["pivot_volume_ratio_20"]),
        "signal_volume_ratio_20": float(features["signal_volume_ratio_20"]),
        "post_pivot_volume_vs_pivot": float(
            features["post_pivot_volume_vs_pivot"]
        ),
        "average_volume_20": float(legacy.average_volume_20),
        "average_turnover_proxy_20": float(
            legacy.average_turnover_proxy_20
        ),
        "signal_calendar_index": int(legacy.calendar_index),
        "signal_segment_id": int(stock.segment_ids[local_index]),
        "raw_overlap": bool(legacy.raw_overlap),
        "compact_rule": cfg.compact_rule,
        "provider_name": snapshot.provider_name,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "prospective_config_hash": cfg.fingerprint(),
        "multi_setup_config_hash": MULTI_CFG.fingerprint(),
        "reversal_config_hash": REVERSAL_CFG.fingerprint(),
        **hashes,
        "execution_mode": cfg.execution_mode,
        "is_actual_order": False,
        "is_actual_fill": False,
    }


def scan_snapshot(
    snapshot: MarketDataSnapshot,
    signal_date: str,
    cfg: Config = CFG,
) -> ScanResult:
    """Causally replay parent N_RETEST and emit only accepted compact T rows.

    The provider must already be physically truncated at T. Non-compact
    accepted parent signals still update the frozen parent cooldown.
    """

    assert_frozen_contract(cfg)
    target = normalize_date(signal_date)
    if snapshot.data_through_date != target:
        raise RuntimeError("signal snapshot must be loaded exactly through T")
    if target not in snapshot.benchmark.calendar:
        raise RuntimeError("target session is absent from the market calendar")
    if any(day > target for day in snapshot.benchmark.calendar):
        raise RuntimeError("benchmark contains data after T")

    raw_target = 0
    accepted_target = 0
    target_bars = 0
    rows: list[dict] = []
    cooldown = REVERSAL_CFG.causal_cooldown_sessions

    for stock in snapshot.prepared_stocks:
        if any(bar.date > target for bar in stock.bars):
            raise RuntimeError(f"provider leaked post-T bar for {stock.code}")
        if any(bar.date == target for bar in stock.bars):
            target_bars += 1
        last_accepted_parent: int | None = None
        for local_index in range(
            REVERSAL_CFG.feature_lookback_sessions, len(stock.bars)
        ):
            bar = stock.bars[local_index]
            if bar.date > target:
                raise RuntimeError(f"provider leaked post-T bar for {stock.code}")
            legacy = build_pattern_observation(
                stock, local_index, snapshot.benchmark, REVERSAL_CFG
            )
            if legacy is None or legacy.pattern != cfg.parent_setup:
                continue
            if bar.date == target:
                raw_target += 1
            accepted = last_accepted_parent is None or (
                legacy.calendar_index - last_accepted_parent >= cooldown
            )
            if not accepted:
                continue
            last_accepted_parent = legacy.calendar_index
            if bar.date != target:
                continue
            accepted_target += 1
            if is_compact_retest(legacy.geometry, MULTI_CFG):
                rows.append(
                    _signal_row(stock, local_index, legacy, snapshot, cfg)
                )

    rows.sort(key=lambda row: (row["stock_id"], row["signal_date"], row["setup"]))
    return ScanResult(
        signal_date=target,
        signals=tuple(rows),
        raw_n_retest_count=raw_target,
        accepted_n_retest_count=accepted_target,
        compact_count=len(rows),
        stocks_with_target_bar=target_bars,
    )
