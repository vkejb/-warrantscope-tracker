"""Causal, paper-only recovery and net-MFE exit variants."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mfe_profit_protection_study_v01.analysis import ResearchTrade


@dataclass(frozen=True, slots=True)
class BufferedExitPolicy:
    name: str
    activation_r: float
    initial_lock_r: float
    hard_stop_net_twd: float = 3500.0
    checkpoint_seconds: int = 120
    checkpoint_loss_r: float = 0.20
    checkpoint_maximum_mfe_r: float = 0.10
    checkpoint_maximum_recovery_r: float = 0.20

    def __post_init__(self) -> None:
        if not 0 <= self.initial_lock_r < self.activation_r:
            raise ValueError("initial lock must be non-negative and below activation")
        if self.hard_stop_net_twd <= 0 or self.checkpoint_seconds <= 0:
            raise ValueError("hard stop and checkpoint must be positive")


POLICIES = (
    BufferedExitPolicy("RECOVERY_NET_MFE_BUFFER_0_30_SHADOW", 0.75, 0.30),
    BufferedExitPolicy("RECOVERY_NET_MFE_BUFFER_0_40_SHADOW", 0.75, 0.40),
)


@dataclass(frozen=True, slots=True)
class ExistingExit:
    at: datetime
    price: float
    net_pnl: float
    reason: str


@dataclass(frozen=True, slots=True)
class BufferedExitResult:
    exit_time: datetime
    exit_price: float
    net_pnl: float
    exit_reason: str
    holding_seconds: float
    mfe_net_pnl: float
    mae_net_pnl: float
    mfe_activation_time: datetime | None
    max_locked_profit_r: float | None
    checkpoint_action: str
    post_exit_best_net_pnl: float
    post_exit_worst_net_pnl: float


def candidate_locked_r(policy: BufferedExitPolicy, mfe_net_r: float) -> float | None:
    if mfe_net_r < policy.activation_r:
        return None
    if mfe_net_r < 1.5:
        return policy.initial_lock_r
    if mfe_net_r < 2.0:
        return max(policy.initial_lock_r, 0.50 * mfe_net_r)
    if mfe_net_r < 3.0:
        return max(policy.initial_lock_r, 0.60 * mfe_net_r)
    return max(policy.initial_lock_r, 0.70 * mfe_net_r)


def simulate_buffered_exit(
    trade: ResearchTrade,
    policy: BufferedExitPolicy,
    *,
    existing_exit: ExistingExit | None = None,
) -> BufferedExitResult:
    """Replay one fixed entry; existing non-MFE exits remain authoritative."""
    if not trade.points:
        raise ValueError("paper shadow trade requires at least one path point")
    checkpoint_at = trade.entry_time + timedelta(seconds=policy.checkpoint_seconds)
    checkpoint_evaluated = False
    checkpoint_action = "NO_CHECKPOINT"
    mfe = mae = trade.pnl_at_price(trade.entry_price)
    locked_r: float | None = None
    activation_time = None
    selected: tuple[int, datetime, float, float, str] | None = None

    for index, point in enumerate(trade.points):
        pnl = float(point.projected_net_pnl)
        mfe = max(mfe, pnl)
        mae = min(mae, pnl)
        candidate = candidate_locked_r(policy, max(0.0, mfe / policy.hard_stop_net_twd))
        if candidate is not None:
            if activation_time is None:
                activation_time = point.at
            locked_r = candidate if locked_r is None else max(locked_r, candidate)

        reason = None
        price = float(point.exit_price)
        realized = pnl
        at = point.at
        if pnl <= -policy.hard_stop_net_twd:
            reason = "STOP_LOSS"
        elif locked_r is not None and pnl <= locked_r * policy.hard_stop_net_twd:
            reason = "BUFFERED_NET_MFE_PROFIT_PROTECTION"
        elif not checkpoint_evaluated and point.at >= checkpoint_at:
            checkpoint_evaluated = True
            recovery = pnl - mae
            triggered = (
                pnl <= -policy.checkpoint_loss_r * policy.hard_stop_net_twd
                and mfe <= policy.checkpoint_maximum_mfe_r * policy.hard_stop_net_twd
                and recovery <= policy.checkpoint_maximum_recovery_r * policy.hard_stop_net_twd
            )
            checkpoint_action = "EXIT" if triggered else "HOLD"
            if triggered:
                reason = "RECOVERY_AWARE_EARLY_FAILURE"

        # The variant replaces only the existing price-R MFE exit. Reversal,
        # hard exit and any other production exit remain valid competitors.
        if (
            reason is None
            and existing_exit is not None
            and existing_exit.reason != "MFE_PROFIT_PROTECTION"
            and point.at >= existing_exit.at
        ):
            reason = existing_exit.reason
            price = existing_exit.price
            realized = existing_exit.net_pnl
            at = existing_exit.at
        if reason is None and index == len(trade.points) - 1:
            reason = "HARD_EXIT"
        if reason is not None:
            selected = (index, at, price, realized, reason)
            break

    if selected is None:
        raise RuntimeError(f"paper exit was not scorable: {trade.trade_id}/{policy.name}")
    index, at, price, realized, reason = selected
    later = trade.points[index + 1:]
    return BufferedExitResult(
        exit_time=at,
        exit_price=price,
        net_pnl=realized,
        exit_reason=reason,
        holding_seconds=(at - trade.entry_time).total_seconds(),
        mfe_net_pnl=mfe,
        mae_net_pnl=mae,
        mfe_activation_time=activation_time,
        max_locked_profit_r=locked_r,
        checkpoint_action=checkpoint_action,
        post_exit_best_net_pnl=max(
            (float(item.projected_net_pnl) for item in later), default=realized,
        ),
        post_exit_worst_net_pnl=min(
            (float(item.projected_net_pnl) for item in later), default=realized,
        ),
    )


def result_dict(result: BufferedExitResult) -> dict[str, Any]:
    return {
        "exit_time": result.exit_time.isoformat(),
        "exit_price": result.exit_price,
        "net_pnl": result.net_pnl,
        "exit_reason": result.exit_reason,
        "holding_seconds": result.holding_seconds,
        "mfe_net_pnl_at_exit": result.mfe_net_pnl,
        "mae_net_pnl_at_exit": result.mae_net_pnl,
        "mfe_activation_time": (
            result.mfe_activation_time.isoformat()
            if result.mfe_activation_time else None
        ),
        "max_locked_profit_r": result.max_locked_profit_r,
        "checkpoint_action": result.checkpoint_action,
        "post_exit_best_net_pnl": result.post_exit_best_net_pnl,
        "post_exit_worst_net_pnl": result.post_exit_worst_net_pnl,
    }
