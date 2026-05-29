from __future__ import annotations

from typing import Any, Dict


def build_portfolio_posture(
    market_state: Dict[str, Any],
    safety_state: Dict[str, Any],
    current_book: Dict[str, Any],
    portfolio_limits: Dict[str, Any],
    control_feedback: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    regime = str(market_state.get("market_regime", "neutral") or "neutral")
    system_mode = str(safety_state.get("system_mode", "NORMAL") or "NORMAL").upper()
    total_cap = float(portfolio_limits.get("total_exposure_cap", 1.0) or 1.0)

    preset = {
        "risk_on": {"new_frac": 0.34, "add_frac": 0.34, "replacement_aggressiveness": 0.76, "defensive_bias": 0.18, "rebalance_mode": "expand"},
        "neutral": {"new_frac": 0.24, "add_frac": 0.28, "replacement_aggressiveness": 0.60, "defensive_bias": 0.38, "rebalance_mode": "neutral"},
        "risk_off": {"new_frac": 0.12, "add_frac": 0.22, "replacement_aggressiveness": 0.42, "defensive_bias": 0.70, "rebalance_mode": "defend"},
        "panic": {"new_frac": 0.00, "add_frac": 0.08, "replacement_aggressiveness": 0.10, "defensive_bias": 0.95, "rebalance_mode": "reduce_only"},
    }.get(regime, {"new_frac": 0.20, "add_frac": 0.25, "replacement_aggressiveness": 0.55, "defensive_bias": 0.45, "rebalance_mode": "neutral"})

    posture = {
        "market_regime": regime,
        "system_mode": system_mode,
        "market_safety_regime": str(safety_state.get("market_safety_regime", "") or ""),
        "manual_halt": bool(safety_state.get("manual_halt", False)),
        "manual_reduce_only": bool(safety_state.get("manual_reduce_only", False)),
        "total_exposure_cap": total_cap,
        "new_entry_budget": round(total_cap * float(preset["new_frac"]), 6),
        "add_budget": round(total_cap * float(preset["add_frac"]), 6),
        "defensive_bias": float(preset["defensive_bias"]),
        "replacement_aggressiveness": float(preset["replacement_aggressiveness"]),
        "rebalance_mode": str(preset["rebalance_mode"]),
        "current_position_count": int(current_book.get("current_position_count", 0) or 0),
        "current_target_weight_sum": float(current_book.get("current_target_weight_sum", 0.0) or 0.0),
        "weak_existing_count": int(current_book.get("weak_existing_count", 0) or 0),
    }
    if bool(safety_state.get("manual_halt", False)) or bool(safety_state.get("manual_reduce_only", False)) or regime == "panic":
        posture["new_entry_budget"] = 0.0
        posture["replacement_aggressiveness"] = min(float(posture["replacement_aggressiveness"]), 0.20)
        posture["rebalance_mode"] = "reduce_only"
    elif system_mode in {"HALT", "DEGRADED"}:
        posture["new_entry_budget"] = round(float(posture["new_entry_budget"]) * 0.65, 6)
        posture["replacement_aggressiveness"] = min(float(posture["replacement_aggressiveness"]), 0.32)
        posture["rebalance_mode"] = "defend"
    if int(current_book.get("weak_existing_count", 0) or 0) >= max(3, int(current_book.get("current_position_count", 0) or 0) // 3):
        posture["replacement_aggressiveness"] = min(1.0, float(posture["replacement_aggressiveness"]) + 0.10)
    feedback = dict(control_feedback or {})
    new_entry_completion = float(feedback.get("recent_new_entry_completion_ratio", 0.0) or 0.0)
    add_completion = float(feedback.get("recent_add_completion_ratio", 0.0) or 0.0)
    persistent_gap = float(feedback.get("persistent_gap_ratio", 0.0) or 0.0)
    truncation = float(feedback.get("turnover_truncation_ratio", 0.0) or 0.0)
    median_completion_hours = float(feedback.get("median_time_to_completion_hours", 0.0) or 0.0)
    partial_stuck_ratio = float(feedback.get("partial_stuck_symbol_ratio", 0.0) or 0.0)
    convergence_score = float(feedback.get("release_convergence_score", 0.0) or 0.0)
    replacement_churn = float(feedback.get("replacement_churn_score", 0.0) or 0.0)
    if new_entry_completion > 0:
        posture["new_entry_budget"] = round(float(posture["new_entry_budget"]) * (0.65 + min(new_entry_completion, 1.0) * 0.55), 6)
    if add_completion > 0:
        posture["add_budget"] = round(float(posture["add_budget"]) * (0.70 + min(add_completion, 1.0) * 0.45), 6)
    if persistent_gap >= 0.16:
        posture["replacement_aggressiveness"] = min(float(posture["replacement_aggressiveness"]), 0.28)
        posture["add_budget"] = round(float(posture["add_budget"]) * 0.82, 6)
    if truncation >= 0.45:
        posture["new_entry_budget"] = round(float(posture["new_entry_budget"]) * 0.80, 6)
    if median_completion_hours >= 12:
        posture["new_entry_budget"] = round(float(posture["new_entry_budget"]) * 0.88, 6)
        posture["add_budget"] = round(float(posture["add_budget"]) * 0.92, 6)
    if partial_stuck_ratio >= 0.35:
        posture["replacement_aggressiveness"] = min(float(posture["replacement_aggressiveness"]), 0.24)
    if convergence_score >= 0.72:
        posture["add_budget"] = round(float(posture["add_budget"]) * 1.05, 6)
    if replacement_churn >= 0.18:
        posture["replacement_aggressiveness"] = min(float(posture["replacement_aggressiveness"]), 0.30)
    posture["control_feedback"] = {
        "recent_new_entry_completion_ratio": round(new_entry_completion, 6),
        "recent_add_completion_ratio": round(add_completion, 6),
        "persistent_gap_ratio": round(persistent_gap, 6),
        "turnover_truncation_ratio": round(truncation, 6),
        "median_time_to_completion_hours": round(median_completion_hours, 6),
        "partial_stuck_symbol_ratio": round(partial_stuck_ratio, 6),
        "release_convergence_score": round(convergence_score, 6),
        "replacement_churn_score": round(replacement_churn, 6),
    }
    return posture
