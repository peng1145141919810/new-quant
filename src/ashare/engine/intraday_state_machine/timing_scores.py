from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return float(max(low, min(high, value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if pd.isna(out):
        return float(default)
    return float(out)


def _pos(value: Any, scale: float, cap: float) -> float:
    return _clip(_to_float(value, 0.0) / max(scale, 1e-9), 0.0, cap)


def _buy_posture_component(row: Dict[str, Any], safety_mode: str, market_state: Dict[str, Any]) -> float:
    lifecycle = str(row.get("source_lifecycle_state", "") or "").strip().lower()
    base = {
        "pilot": 0.72,
        "build": 0.88,
        "hold": 0.55,
        "trim": 0.18,
        "exit": 0.08,
        "watch": 0.05,
    }.get(lifecycle, 0.1)
    policy = str(market_state.get("new_position_policy", "allow") or "allow").strip().lower()
    if policy in {"no_new_positions", "reduce_only"} and lifecycle in {"pilot", "build"}:
        base *= 0.1
    safety = str(safety_mode or "").upper()
    if safety == "CAUTION":
        base *= 0.72
    elif safety in {"PANIC", "HALT"}:
        base = 0.0
    return _clip(base)


def _sell_risk_pressure_component(row: Dict[str, Any], safety_mode: str) -> float:
    lifecycle = str(row.get("source_lifecycle_state", "") or "").strip().lower()
    gap = max(_to_float(row.get("actual_weight"), 0.0) - _to_float(row.get("target_weight"), 0.0), 0.0)
    lifecycle_term = {
        "exit": 0.95,
        "trim": 0.82,
        "hold": 0.46,
        "build": 0.28,
        "pilot": 0.18,
        "watch": 0.05,
    }.get(lifecycle, 0.1)
    gap_term = _clip(gap / 0.03, 0.0, 0.45)
    safety = str(safety_mode or "").upper()
    if safety == "CAUTION":
        lifecycle_term += 0.08
    elif safety == "PANIC":
        lifecycle_term += 0.22
    elif safety == "HALT":
        lifecycle_term = 1.0
    return _clip(lifecycle_term + gap_term)


def compute_timing_scores(
    *,
    feature_frame: pd.DataFrame,
    market_state: Dict[str, Any],
    safety_mode: str,
) -> pd.DataFrame:
    if feature_frame is None or feature_frame.empty:
        return pd.DataFrame()

    rows = []
    for _, series in feature_frame.iterrows():
        row = dict(series.to_dict())
        last_ret = _to_float(row.get("last_price_vs_prev_close"), 0.0)
        last_vs_open = _to_float(row.get("last_price_vs_open"), last_ret)
        last_vs_vwap = _to_float(row.get("last_price_vs_vwap"), 0.0)
        rel_index = _to_float(row.get("relative_strength_vs_index"), 0.0)
        rel_industry = _to_float(row.get("relative_strength_vs_industry"), 0.0)
        amount_ratio = _to_float(row.get("intraday_amount_ratio"), 1.0)
        volume_ratio = _to_float(row.get("intraday_volume_ratio"), 1.0)
        amount_acceleration = _to_float(row.get("amount_acceleration"), 1.0)
        turnover_acceleration = _to_float(row.get("turnover_acceleration"), 1.0)
        proxy_spread = _to_float(row.get("proxy_spread_pct"), 0.0)
        proxy_tick_imbalance = _to_float(row.get("proxy_tick_imbalance"), 0.0)
        proxy_heat = _to_float(row.get("proxy_market_heat_score"), 0.0)
        current_high_gap = _to_float(row.get("distance_from_day_high_pct"), 0.0)
        current_low_bounce = _to_float(row.get("intraday_return_from_low"), 0.0)
        tech_final_score = _to_float(row.get("tech_tech_final_score"), 0.5)
        tech_allow_entry = bool(row.get("tech_tech_allow_entry", True))
        overheat_penalty = 0.0
        if last_ret >= 0.06:
            overheat_penalty += 0.24
        if current_high_gap <= 0.005 and last_ret > 0.03:
            overheat_penalty += 0.12
        if bool(row.get("message_veto_flag", False)):
            overheat_penalty += 0.55
        if bool(row.get("snapshot_stale", False)):
            overheat_penalty += 0.12
        if proxy_spread >= 0.02:
            overheat_penalty += 0.14
        if not tech_allow_entry:
            overheat_penalty += 0.14

        buy_technical = 0.0
        buy_technical += 0.16 if bool(row.get("vwap_reclaim_flag", False)) else 0.0
        buy_technical += 0.16 if bool(row.get("intraday_reversal_up_flag", False)) else 0.0
        buy_technical += 0.12 if bool(row.get("opening_range_breakout_up", False)) else 0.0
        buy_technical += _pos(last_vs_open, 0.03, 0.14)
        buy_technical += _pos(rel_index, 0.025, 0.14)
        buy_technical += _pos(rel_industry, 0.02, 0.10)
        buy_technical += _pos(proxy_tick_imbalance, 0.25, 0.10)
        buy_technical += _clip(tech_final_score, 0.0, 1.0) * 0.12
        buy_technical = _clip(buy_technical)

        buy_flow = 0.0
        buy_flow += _pos(amount_ratio - 1.0, 0.8, 0.22)
        buy_flow += _pos(volume_ratio - 1.0, 0.8, 0.18)
        buy_flow += _pos(amount_acceleration - 1.0, 0.8, 0.10)
        buy_flow += 0.12 if bool(row.get("price_up_amount_up_flag", False)) else 0.0
        buy_flow += 0.10 if bool(row.get("volume_confirmation_flag", False)) else 0.0
        buy_flow += 0.08 if bool(row.get("proxy_top_list_hit", False)) else 0.0
        buy_flow += _clip(proxy_heat, 0.0, 1.0) * 0.10
        buy_flow = _clip(buy_flow)

        buy_posture = _buy_posture_component(row, safety_mode=safety_mode, market_state=market_state)
        buy_penalty = _clip(overheat_penalty)
        buy_score = _clip(0.46 * buy_technical + 0.24 * buy_flow + 0.22 * buy_posture - 0.16 * buy_penalty)

        sell_technical = 0.0
        sell_technical += 0.16 if bool(row.get("vwap_break_flag", False)) else 0.0
        sell_technical += 0.14 if bool(row.get("intraday_reversal_down_flag", False)) else 0.0
        sell_technical += 0.14 if bool(row.get("morning_high_fail_flag", False)) else 0.0
        sell_technical += _pos(-last_vs_open, 0.03, 0.14)
        sell_technical += _pos(-rel_index, 0.025, 0.14)
        sell_technical += _pos(current_low_bounce * -1.0, 0.03, 0.10)
        sell_technical += _pos(_to_float(row.get("distance_from_day_high_pct"), 0.0), 0.03, 0.10)
        sell_technical += _pos(-proxy_tick_imbalance, 0.25, 0.08)
        sell_technical = _clip(sell_technical)

        sell_flow = 0.0
        sell_flow += 0.15 if bool(row.get("price_down_amount_up_flag", False)) else 0.0
        sell_flow += 0.10 if bool(row.get("price_up_amount_down_flag", False)) else 0.0
        sell_flow += _pos(amount_ratio - 1.0, 1.0, 0.16)
        sell_flow += _pos(turnover_acceleration - 1.0, 1.0, 0.14)
        sell_flow += 0.10 if proxy_spread >= 0.015 else 0.0
        sell_flow = _clip(sell_flow)

        sell_risk = _sell_risk_pressure_component(row, safety_mode=safety_mode)
        sell_penalty = 0.0
        if last_ret <= -0.06:
            sell_penalty += 0.28
        if bool(row.get("message_veto_flag", False)):
            sell_penalty += 0.10
        if bool(row.get("snapshot_stale", False)):
            sell_penalty += 0.12
        sell_penalty = _clip(sell_penalty)
        sell_score = _clip(0.40 * sell_technical + 0.20 * sell_flow + 0.30 * sell_risk - 0.10 * sell_penalty)

        rows.append(
            {
                "stock_code": str(row.get("stock_code", "") or ""),
                "buy_technical_component": round(buy_technical, 6),
                "buy_flow_confirmation_component": round(buy_flow, 6),
                "buy_posture_component": round(buy_posture, 6),
                "buy_penalty_component": round(buy_penalty, 6),
                "buy_timing_score": round(buy_score, 6),
                "sell_technical_component": round(sell_technical, 6),
                "sell_flow_confirmation_component": round(sell_flow, 6),
                "sell_risk_pressure_component": round(sell_risk, 6),
                "sell_penalty_component": round(sell_penalty, 6),
                "sell_timing_score": round(sell_score, 6),
            }
        )
    return pd.DataFrame(rows)
