from __future__ import annotations

from typing import Any, Dict, List


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value or {})


def build_execution_management_decision(
    *,
    config: Dict[str, Any],
    scheduler_verdict: Dict[str, Any],
    release_doc: Dict[str, Any],
    portfolio_summary: Dict[str, Any],
    market_state: Dict[str, Any],
    account_snapshot: Dict[str, Any],
    harvest_risk: Dict[str, Any],
    global_objective: Dict[str, Any],
) -> Dict[str, Any]:
    execution_policy = _dict(config.get("execution_policy"))
    ems_cfg = _dict(config.get("execution_management"))
    plan = _dict(scheduler_verdict.get("execution_plan"))
    objective_scores = _dict(global_objective.get("scores"))
    objective_flags = set(list(global_objective.get("hard_flags", []) or []))
    recommended_budget = _dict(global_objective.get("recommended_budget"))
    harvest_score = _float(harvest_risk.get("harvest_risk_score"), 0.0)
    evidence_score = _float(objective_scores.get("evidence"), 0.0)
    execution_score = _float(objective_scores.get("execution"), 0.0)
    overall_score = _float(objective_scores.get("overall"), 0.0)
    shadow_only = bool(recommended_budget.get("shadow_only", False))
    final_verdict = _text(scheduler_verdict.get("final_verdict")).lower()
    posture = "stage"
    pacing = "balanced"
    urgency = "normal"
    allowed_actions: List[str] = ["stage_orders", "monitor"]
    rationale: List[str] = []

    if final_verdict in {"block", "defer"}:
        posture = "standby"
        pacing = "hold"
        urgency = "none"
        allowed_actions = ["monitor"]
        rationale.append("scheduler_verdict_non_dispatch")
    elif final_verdict == "reduce_only" or bool(plan.get("reduce_only", False)):
        posture = "reduce_only"
        pacing = "slow"
        urgency = "normal"
        allowed_actions = ["reduce_positions", "cancel_aggressive_adds", "monitor"]
        rationale.append("scheduler_reduce_only")
    elif shadow_only:
        posture = "shadow"
        pacing = "observe"
        urgency = "none"
        allowed_actions = ["shadow_only", "monitor"]
        rationale.append("objective_shadow_only")
    elif harvest_score >= 0.70:
        posture = "defensive"
        pacing = "patient"
        urgency = "low"
        allowed_actions = ["stage_orders", "delay_entries", "monitor"]
        rationale.append("harvest_risk_high")
    elif evidence_score >= 0.60 and execution_score >= 0.55 and overall_score >= 0.65:
        posture = "active"
        pacing = "balanced"
        urgency = "high" if _text(market_state.get("new_position_policy")).lower() == "open" else "normal"
        allowed_actions = ["stage_orders", "split_orders", "monitor"]
        rationale.append("objective_supportive")

    if _float(account_snapshot.get("cash"), 0.0) <= 0 or _float(account_snapshot.get("nav"), 0.0) <= 0:
        rationale.append("account_not_funded")
        if posture not in {"standby", "shadow"}:
            posture = "defensive"
            allowed_actions = ["monitor"]
            pacing = "hold"
            urgency = "none"
    if {"guardrail_penalty_above_ceiling", "incremental_value_below_floor"} & objective_flags:
        posture = "shadow" if posture not in {"reduce_only", "standby"} else posture
        pacing = "observe" if posture == "shadow" else pacing
        urgency = "none" if posture == "shadow" else urgency
        allowed_actions = ["shadow_only", "monitor"] if posture == "shadow" else allowed_actions
        rationale.append("econometric_guardrail_stop")

    max_child_order_ratio = min(max(_float(ems_cfg.get("max_child_order_ratio"), 0.20), 0.01), 1.0)
    if posture in {"defensive", "reduce_only"}:
        max_child_order_ratio = min(max_child_order_ratio, 0.10)
    elif posture == "active":
        max_child_order_ratio = min(max_child_order_ratio * 1.25, 0.30)

    return {
        "version": "execution_management_v1",
        "posture": posture,
        "pacing": pacing,
        "urgency": urgency,
        "allowed_actions": allowed_actions,
        "rationale": rationale,
        "policy": {
            "namespace": _text(execution_policy.get("namespace")) or "main",
            "shadow_run": bool(execution_policy.get("shadow_run", False)),
            "max_child_order_ratio": max_child_order_ratio,
            "staged_entry_delay_seconds": max(int(_float(ems_cfg.get("staged_entry_delay_seconds"), 45)), 0),
            "allow_cancel_replace": bool(ems_cfg.get("allow_cancel_replace", True)),
        },
        "signals": {
            "overall_objective_score": overall_score,
            "evidence_score": evidence_score,
            "execution_score": execution_score,
            "harvest_risk_score": harvest_score,
            "market_regime": _text(market_state.get("market_regime")),
            "release_id": _text(release_doc.get("release_id")),
            "portfolio_names": int(_float(_dict(portfolio_summary).get("n_names"), 0.0)),
        },
    }
