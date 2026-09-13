from __future__ import annotations

import statistics

from surge_event_study_v01.models import PreparedStock

from .config import CFG, SETUP_DEFINITIONS, Config


FAMILY_BY_SETUP = {setup_id: family for setup_id, family, _ in SETUP_DEFINITIONS}


def _true_range(stock: PreparedStock, position: int) -> float:
    bar = stock.bars[position]
    previous_close = stock.bars[position - 1].close
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def setup_flags(stock: PreparedStock, position: int, cfg: Config = CFG) -> dict[str, bool]:
    """Evaluate every preregistered setup using T and earlier bars only."""

    flags = {setup_id: False for setup_id, _, _ in SETUP_DEFINITIONS}
    if position < cfg.minimum_history_sessions:
        return flags
    if (
        stock.segment_ids[position - cfg.minimum_history_sessions]
        != stock.segment_ids[position]
        or stock.calendar_indices[position] - stock.calendar_indices[position - cfg.minimum_history_sessions]
        != cfg.minimum_history_sessions
    ):
        return flags

    bars = stock.bars
    current, previous = bars[position], bars[position - 1]
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    prior20_high = max(closes[position - 20 : position])
    prior60_high = max(closes[position - 60 : position])
    prior10_high = max(closes[position - 10 : position])
    median_volume20 = statistics.median(volumes[position - 20 : position])
    volume_ratio = current.volume / median_volume20 if median_volume20 > 0 else 0.0

    ma20 = statistics.fmean(closes[position - 19 : position + 1])
    ma60 = statistics.fmean(closes[position - 59 : position + 1])
    ma20_t_minus_5 = statistics.fmean(closes[position - 24 : position - 4])
    atr20_current = statistics.fmean(
        _true_range(stock, index) for index in range(position - 19, position + 1)
    )
    trend = ma20 > ma60 and ma20 > ma20_t_minus_5

    atr5_prior = statistics.fmean(
        _true_range(stock, index) for index in range(position - 5, position)
    )
    atr20_prior = statistics.fmean(
        _true_range(stock, index) for index in range(position - 20, position)
    )
    prior5_range = statistics.fmean(
        (bars[index].high - bars[index].low) / bars[index - 1].close
        for index in range(position - 5, position)
    )
    prior20_range = statistics.fmean(
        (bars[index].high - bars[index].low) / bars[index - 1].close
        for index in range(position - 20, position)
    )
    gap = current.open / previous.close - 1.0
    close_location = (
        (current.close - current.low) / (current.high - current.low)
        if current.high > current.low
        else 0.5
    )

    flags.update(
        A1=current.close > prior20_high,
        A2=current.close > prior20_high and volume_ratio >= 1.5,
        A3=current.close > prior60_high,
        A4=current.close > prior60_high and volume_ratio >= 1.5,
        B1=trend and abs(current.close - ma20) <= 0.50 * atr20_current and current.close >= ma20,
        B2=trend and current.low <= ma20 and current.close >= ma20,
        B3=(
            trend
            and ma20 - 0.50 * atr20_current <= current.close <= ma20 + 0.25 * atr20_current
            and current.close > previous.close
        ),
        C1=current.close <= ma20 - 1.0 * atr20_current,
        C2=current.close <= ma20 - 1.5 * atr20_current,
        C3=current.close <= ma20 - 1.0 * atr20_current and current.close > ma60,
        D1=atr20_prior > 0 and atr5_prior / atr20_prior <= 0.75 and current.close > prior10_high,
        D2=atr20_prior > 0 and atr5_prior / atr20_prior <= 0.85 and current.close > prior20_high,
        D3=prior5_range < prior20_range * 0.70 and current.close > prior10_high,
        E1=gap >= 0.02 and current.close > current.open and close_location >= 0.70,
        E2=gap >= 0.02 and current.close < current.open and current.close > previous.close,
        E3=gap <= -0.02 and current.close > current.open and current.close > previous.close,
    )
    return flags
