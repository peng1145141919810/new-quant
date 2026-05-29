from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class TacticalPolicy:
    max_daily_turnover_ratio: float
    max_symbol_add_ratio: float
    max_symbol_reduce_ratio: float
    buy_cooldown_minutes: int
    sell_cooldown_minutes: int
    allow_add_on_snapshot_degraded: bool
    allow_reduce_on_snapshot_degraded: bool
    enable_time_stop: bool
    enable_take_profit: bool
    enable_stop_loss: bool
    enable_t_overlay: bool
    enable_tactical_add: bool
    tp_soft_pct: float
    tp_hard_pct: float
    sl_soft_pct: float
    sl_hard_pct: float
    time_stop_minutes: int
    reason_thresholds: Dict[str, Any]


def load_tactical_policy(config: Dict[str, Any]) -> TacticalPolicy:
    raw = dict(config.get("intraday_tactics", {}) or {})
    th = dict(raw.get("reason_thresholds", {}) or {})
    return TacticalPolicy(
        max_daily_turnover_ratio=float(raw.get("max_daily_turnover_ratio", 0.12) or 0.12),
        max_symbol_add_ratio=float(raw.get("max_symbol_add_ratio", 0.06) or 0.06),
        max_symbol_reduce_ratio=float(raw.get("max_symbol_reduce_ratio", 0.25) or 0.25),
        buy_cooldown_minutes=int(raw.get("buy_cooldown_minutes", 18) or 18),
        sell_cooldown_minutes=int(raw.get("sell_cooldown_minutes", 5) or 5),
        allow_add_on_snapshot_degraded=bool(raw.get("allow_add_on_snapshot_degraded", True)),
        allow_reduce_on_snapshot_degraded=bool(raw.get("allow_reduce_on_snapshot_degraded", True)),
        enable_time_stop=bool(raw.get("enable_time_stop", True)),
        enable_take_profit=bool(raw.get("enable_take_profit", True)),
        enable_stop_loss=bool(raw.get("enable_stop_loss", True)),
        enable_t_overlay=bool(raw.get("enable_t_overlay", True)),
        enable_tactical_add=bool(raw.get("enable_tactical_add", True)),
        tp_soft_pct=float(th.get("take_profit_soft_pct", 0.035) or 0.035),
        tp_hard_pct=float(th.get("take_profit_hard_pct", 0.08) or 0.08),
        sl_soft_pct=float(th.get("stop_loss_soft_pct", 0.025) or 0.025),
        sl_hard_pct=float(th.get("stop_loss_hard_pct", 0.06) or 0.06),
        time_stop_minutes=int(th.get("time_stop_minutes", 120) or 120),
        reason_thresholds=th,
    )
