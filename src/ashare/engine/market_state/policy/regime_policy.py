from __future__ import annotations

from typing import Any, Dict


def _pick_market_regime(score: float, thresholds: Dict[str, Any]) -> str:
    panic = float(thresholds.get("panic_max", -0.55) or -0.55)
    risk_off = float(thresholds.get("risk_off_max", -0.15) or -0.15)
    risk_on = float(thresholds.get("risk_on_min", 0.30) or 0.30)
    if score <= panic:
        return "panic"
    if score <= risk_off:
        return "risk_off"
    if score >= risk_on:
        return "risk_on"
    return "neutral"


def _style_bias(metrics: Dict[str, Any], mechanism_scores: Dict[str, float]) -> str:
    size_spread = float(metrics.get("size_spread_1d", 0.0) or 0.0)
    top_mechanism = max(mechanism_scores, key=mechanism_scores.get) if mechanism_scores else ""
    if top_mechanism == "price_inventory":
        return "cyclical"
    if top_mechanism == "macro_style":
        return "defensive"
    if size_spread >= 0.01:
        return "growth"
    if size_spread <= -0.01:
        return "defensive"
    return "balanced"


def _mechanism_bias(mechanism_scores: Dict[str, float], bias_threshold: float) -> str:
    if not mechanism_scores:
        return "balanced"
    top = max(mechanism_scores, key=mechanism_scores.get)
    top_score = float(mechanism_scores.get(top, 0.0) or 0.0)
    others = [float(v or 0.0) for k, v in mechanism_scores.items() if k != top]
    next_best = max(others) if others else 0.0
    if top_score - next_best < float(bias_threshold):
        return "balanced"
    return str(top)


def build_regime_policy(scores: Dict[str, Any], feature_snapshot: Dict[str, Any], config_payload: Dict[str, Any]) -> Dict[str, Any]:
    regime = _pick_market_regime(
        score=float(scores.get("market_regime_score", 0.0) or 0.0),
        thresholds=dict(config_payload.get("regime_thresholds", {}) or {}),
    )
    policies = dict(config_payload.get("regime_policy", {}) or {})
    policy = dict(policies.get(regime, {}) or {})
    mechanism_scores = dict(scores.get("mechanism_scores", {}) or {})
    metrics = dict(feature_snapshot.get("metrics", {}) or {})
    style_bias = _style_bias(metrics=metrics, mechanism_scores=mechanism_scores)
    mechanism_bias = _mechanism_bias(
        mechanism_scores=mechanism_scores,
        bias_threshold=float(config_payload.get("mechanism_bias_threshold", 0.08) or 0.08),
    )
    mechanism_multipliers: Dict[str, float] = {
        "trend_capex": 1.0,
        "price_inventory": 1.0,
        "macro_style": 1.0,
        "balanced": 1.0,
    }
    if mechanism_bias != "balanced":
        for key in ["trend_capex", "price_inventory", "macro_style"]:
            mechanism_multipliers[key] = 0.92
        mechanism_multipliers[mechanism_bias] = 1.10
    return {
        "market_regime": regime,
        "style_bias": style_bias,
        "mechanism_bias": mechanism_bias,
        "risk_budget_multiplier": float(policy.get("risk_budget_multiplier", 1.0) or 1.0),
        "turnover_multiplier": float(policy.get("turnover_multiplier", 1.0) or 1.0),
        "entry_strictness": float(policy.get("entry_strictness", 0.5) or 0.5),
        "new_position_policy": str(policy.get("new_position_policy", "allow") or "allow"),
        "de_risk_hint": str(policy.get("de_risk_hint", "") or ""),
        "mechanism_multipliers": mechanism_multipliers,
    }
