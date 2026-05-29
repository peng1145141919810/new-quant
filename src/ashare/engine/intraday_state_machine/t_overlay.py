from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from ..clock_account_snapshot import load_clock_account_snapshot_file
from ..t_audit import resolve_t_execution_policy


T_OVERLAY_STATES = [
    "t_disabled",
    "t_armed",
    "t_sell_leg_done_wait_buyback",
    "t_buy_leg_done_wait_sellback",
    "t_completed",
    "t_frozen",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if pd.isna(out):
        return float(default)
    return float(out)


def _allowed_ratio(row: Dict[str, Any], safety_mode: str, max_ratio: float, policy_max_ratio: float = 0.0) -> float:
    lifecycle = str(row.get("source_lifecycle_state", "") or "").strip().lower()
    base = {
        "hold": 1.0,
        "build": 0.8,
        "trim": 0.55,
        "pilot": 0.35,
    }.get(lifecycle, 0.0)
    safety = str(safety_mode or "").upper()
    if safety == "CAUTION":
        base *= 0.6
    elif safety == "PANIC":
        base *= 0.28
    elif safety == "HALT":
        base = 0.0
    base_ratio = max_ratio if policy_max_ratio <= 0 else min(max_ratio, policy_max_ratio)
    return round(base_ratio * base, 6)


def _previous_state_map(previous_frame: pd.DataFrame, trade_date: str) -> Dict[str, Dict[str, Any]]:
    if previous_frame is None or previous_frame.empty:
        return {}
    current = previous_frame.copy()
    if "trade_date" in current.columns:
        current = current.loc[current["trade_date"].astype(str).eq(str(trade_date or ""))].copy()
    if current.empty or "stock_code" not in current.columns:
        return {}
    current = current.drop_duplicates(subset=["stock_code"], keep="last")
    return {str(row["stock_code"]): dict(row) for _, row in current.iterrows()}


def apply_t_overlay(
    *,
    frame: pd.DataFrame,
    previous_frame: pd.DataFrame,
    current_phase: str,
    current_window: Dict[str, Any],
    config: Dict[str, Any],
    safety_mode: str,
    trade_date: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()

    intraday_cfg = dict(config.get("intraday_state_machine", {}) or {})
    t_cfg = dict(intraday_cfg.get("t_overlay", {}) or {})
    enabled = bool(t_cfg.get("enabled", True))
    max_rounds = int(t_cfg.get("max_rounds_per_symbol_per_day", 1) or 1)
    max_ratio = float(t_cfg.get("max_ratio_per_symbol", 0.2) or 0.2)
    snap = load_clock_account_snapshot_file(config)
    conc = str(snap.get("concentration_risk") or "ok").strip().lower() if snap else "ok"
    spread_cap = 0.02
    conc_ratio_mult = 1.0
    if snap and snap.get("enabled", True) is not False:
        cg = dict(t_cfg.get("concentration_guard", {}) or {})
        if conc == "high":
            spread_cap = float(cg.get("high_risk_max_proxy_spread", 0.015) or 0.015)
            conc_ratio_mult = float(cg.get("high_risk_max_ratio_multiplier", 0.65) or 0.65)
        elif conc == "elevated":
            spread_cap = float(cg.get("elevated_max_proxy_spread", 0.018) or 0.018)
            conc_ratio_mult = float(cg.get("elevated_max_ratio_multiplier", 0.82) or 0.82)
    max_ratio = round(max_ratio * conc_ratio_mult, 6)
    disable_on_panic = bool(t_cfg.get("disable_on_panic", True))
    disable_on_major_event = bool(t_cfg.get("disable_on_major_event", True))
    enable_afternoon_second_leg = bool(dict(intraday_cfg.get("timing_layer", {}) or {}).get("enable_afternoon_second_leg", True))
    previous = _previous_state_map(previous_frame=previous_frame, trade_date=trade_date)

    rows = []
    for _, series in frame.iterrows():
        row = dict(series.to_dict())
        symbol = str(row.get("stock_code", "") or "")
        prev = dict(previous.get(symbol, {}) or {})
        prev_state = str(prev.get("t_overlay_state", "") or "").strip()
        prev_direction = str(prev.get("t_direction", "") or "").strip()
        prev_rounds = int(_to_float(prev.get("t_rounds_used"), 0.0))
        actual_weight = _to_float(row.get("actual_weight"), 0.0)
        buy_score = _to_float(row.get("buy_timing_score"), 0.0)
        sell_score = _to_float(row.get("sell_timing_score"), 0.0)
        timing_state = str(row.get("timing_state", "") or "").strip()
        lifecycle = str(row.get("source_lifecycle_state", "") or "").strip().lower()
        has_old_base = bool(row.get("has_old_base_position", False)) and actual_weight > 0.0
        proxy_spread = _to_float(row.get("proxy_spread_pct"), 0.0)
        proxy_stale = str(row.get("feature_quality_tier", "") or "") == "proxy_stale"
        policy = resolve_t_execution_policy(config=config, row=row, timing_window=current_window.get("name", ""))
        allowed_ratio = _allowed_ratio(
            row=row,
            safety_mode=safety_mode,
            max_ratio=max_ratio,
            policy_max_ratio=_to_float(policy.get("max_t_ratio"), 0.0),
        )
        t_eligible = (
            enabled
            and bool(policy.get("t_allowed", False))
            and has_old_base
            and lifecycle in {"hold", "build", "trim", "pilot"}
            and timing_state not in {"timing_frozen", "reconcile_only"}
            and allowed_ratio > 0.0
            and not proxy_stale
            and proxy_spread < spread_cap
        )
        t_state = "t_disabled"
        t_direction = ""
        t_leg_done = ""
        t_trigger_reason = ""
        t_rounds_used = prev_rounds
        t_transition_flag = False

        if not enabled:
            t_state = "t_disabled"
        elif prev_state == "t_completed":
            t_state = "t_completed"
            t_direction = prev_direction
            t_leg_done = "completed"
        elif str(safety_mode or "").upper() == "HALT":
            t_state = "t_frozen"
            t_trigger_reason = "safety_halt"
        elif disable_on_panic and str(safety_mode or "").upper() == "PANIC":
            t_state = "t_frozen"
            t_trigger_reason = "panic_blocks_t"
        elif disable_on_major_event and bool(row.get("message_veto_flag", False)):
            t_state = "t_frozen"
            t_trigger_reason = str(row.get("message_veto_reason", "") or "major_event_veto")
        elif not t_eligible:
            t_state = "t_disabled"
            if proxy_stale:
                t_trigger_reason = "proxy_snapshot_stale"
            elif proxy_spread >= spread_cap:
                t_trigger_reason = "proxy_spread_too_wide"
        elif prev_state == "t_sell_leg_done_wait_buyback":
            t_state = prev_state
            t_direction = prev_direction or "positive_t"
            t_leg_done = "sell_leg"
            if bool(policy.get("allow_second_leg", True)) and enable_afternoon_second_leg and bool(current_window.get("allow_t_second_leg", False)) and buy_score >= float(config.get("intraday_state_machine", {}).get("timing_layer", {}).get("buy_score_threshold", 0.58) or 0.58):
                t_state = "t_completed"
                t_leg_done = "completed"
                t_trigger_reason = "positive_t_buyback_ready"
                t_rounds_used = min(max_rounds, max(prev_rounds, 1))
                t_transition_flag = True
        elif prev_state == "t_buy_leg_done_wait_sellback":
            t_state = prev_state
            t_direction = prev_direction or "reverse_t"
            t_leg_done = "buy_leg"
            if bool(policy.get("allow_second_leg", True)) and enable_afternoon_second_leg and bool(current_window.get("allow_t_second_leg", False)) and sell_score >= float(config.get("intraday_state_machine", {}).get("timing_layer", {}).get("sell_score_threshold", 0.62) or 0.62):
                t_state = "t_completed"
                t_leg_done = "completed"
                t_trigger_reason = "reverse_t_sellback_ready"
                t_rounds_used = min(max_rounds, max(prev_rounds, 1))
                t_transition_flag = True
        elif prev_rounds >= max_rounds:
            t_state = "t_completed"
            t_leg_done = "completed"
            t_direction = prev_direction
        elif str(current_phase or "") in {"close_reconcile", "postclose_archive"}:
            t_state = "t_frozen"
            t_trigger_reason = "close_reconcile_freeze"
        else:
            t_state = "t_armed"
            if bool(current_window.get("allow_t_first_leg", False)):
                if sell_score >= max(buy_score + 0.06, 0.64):
                    t_state = "t_sell_leg_done_wait_buyback"
                    t_direction = "positive_t"
                    t_leg_done = "sell_leg"
                    t_trigger_reason = "positive_t_sell_leg_ready"
                    t_rounds_used = 1
                    t_transition_flag = True
                elif buy_score >= max(sell_score + 0.06, 0.60):
                    t_state = "t_buy_leg_done_wait_sellback"
                    t_direction = "reverse_t"
                    t_leg_done = "buy_leg"
                    t_trigger_reason = "reverse_t_buy_leg_ready"
                    t_rounds_used = 1
                    t_transition_flag = True

        rows.append(
            {
                "stock_code": symbol,
                "t_overlay_state": t_state,
                "t_direction": t_direction,
                "t_leg_done": t_leg_done,
                "t_allowed_ratio": allowed_ratio,
                "t_eligible": bool(t_eligible),
                "t_triggered": bool(t_transition_flag),
                "t_trigger_reason": t_trigger_reason,
                "t_rounds_used": int(t_rounds_used),
                "policy_t_allowed": bool(policy.get("t_allowed", False)),
                "policy_allow_second_leg": bool(policy.get("allow_second_leg", True)),
                "policy_max_t_ratio": float(policy.get("max_t_ratio", 0.0) or 0.0),
            }
        )
    return pd.DataFrame(rows)
