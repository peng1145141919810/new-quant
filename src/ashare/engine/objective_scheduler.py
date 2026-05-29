from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .runtime_protocol import artifact_identity, build_advice, compact_reason_chain

MAX_SCHEDULER_INPUT_JSON_BYTES = 5 * 1024 * 1024


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value or {})


def _list(value: Any) -> List[Any]:
    return list(value or [])


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    cleaned = {str(k): max(_float(v, 0.0), 0.0) for k, v in dict(weights or {}).items()}
    total = sum(cleaned.values()) or 1.0
    return {k: round(v / total, 6) for k, v in cleaned.items()}


def _scale_route(weights: Dict[str, float], route: str, factor: float) -> None:
    if route in weights:
        weights[route] = max(_float(weights.get(route), 0.0) * float(factor), 0.0)


def _base_cycles_for_profile(profile: str) -> int:
    normalized = _text(profile).lower()
    if normalized == "quick_test":
        return 1
    if normalized == "daily_production":
        return 3
    return 8


def _load_latest_json(path: Path, *, max_bytes: int = MAX_SCHEDULER_INPUT_JSON_BYTES) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if max_bytes > 0 and path.stat().st_size > max_bytes:
            return {}
    except Exception:
        return {}
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _compact_feedback_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    src = _dict(payload)
    keep_keys = [
        "generated_at",
        "available",
        "authority_role",
        "regime",
        "summary",
        "review_summary",
        "metrics",
        "signal_trace",
        "signal_route_weights",
        "route_space_signals",
        "preferred_model_signals",
        "ban_model_signals",
        "research_constraint_signals",
        "portfolio_constraint_signals",
        "route_weights",
        "route_budget",
        "route_space_overrides",
        "preferred_model_families",
        "ban_model_families",
        "research_brain_overrides",
        "strategy_overrides",
        "portfolio_overrides",
        "control_feedback_bridge",
        "mechanism_execution_realization",
        "repeated_non_executable_symbols",
    ]
    out = {key: src.get(key) for key in keep_keys if key in src}
    if isinstance(out.get("signal_trace"), list):
        out["signal_trace"] = out["signal_trace"][:20]
    if isinstance(out.get("mechanism_execution_realization"), list):
        out["mechanism_execution_realization"] = out["mechanism_execution_realization"][:8]
    if isinstance(out.get("repeated_non_executable_symbols"), list):
        out["repeated_non_executable_symbols"] = out["repeated_non_executable_symbols"][:30]
    decision = _dict(src.get("scheduler_budget_decision") or src.get("scheduler_research_decision"))
    if decision:
        out["scheduler_decision_summary"] = _compact_budget_decision(decision)
    return out


def _compact_advisor_chain(advisors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    for item in list(advisors or []):
        advisor = _dict(item)
        compacted.append(
            {
                "advisor": advisor.get("advisor"),
                "category": advisor.get("category"),
                "status": advisor.get("status"),
                "summary": advisor.get("summary"),
                "hard_stop": advisor.get("hard_stop"),
                "reasons": _list(advisor.get("reasons"))[:12],
                "evidence": _dict(advisor.get("evidence")),
            }
        )
    return compacted


def _compact_budget_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    src = _dict(decision)
    return {
        "version": src.get("version"),
        "generated_at": src.get("generated_at"),
        "authority_owner": src.get("authority_owner"),
        "status": src.get("status"),
        "final_verdict": src.get("final_verdict"),
        "policy_posture": src.get("policy_posture"),
        "reason_chain": _list(src.get("reason_chain"))[:60],
        "advisor_chain": _compact_advisor_chain(_list(src.get("advisor_chain"))),
        "research_plan": _dict(src.get("research_plan")),
        "portfolio_construction": _dict(src.get("portfolio_construction")),
        "route_weights": _dict(src.get("route_weights")),
        "route_budget": _dict(src.get("route_budget")),
        "route_space_overrides": _dict(src.get("route_space_overrides")),
        "preferred_model_families": _list(src.get("preferred_model_families")),
        "ban_model_families": _list(src.get("ban_model_families")),
        "research_brain_overrides": _dict(src.get("research_brain_overrides")),
        "strategy_overrides": _dict(src.get("strategy_overrides")),
        "portfolio_overrides": _dict(src.get("portfolio_overrides")),
        "reasons": _list(src.get("reasons"))[:40],
    }


def _allocate_route_budget(route_weights: Dict[str, float], total_budget: int, min_each: int) -> Dict[str, int]:
    weights = _normalize_weights(route_weights)
    if not weights:
        weights = _normalize_weights(
            {"feature": 1.0, "model": 1.0, "training": 1.0, "portfolio": 1.0, "risk": 1.0, "data": 1.0, "hybrid": 1.0}
        )
    budget = {route: int(min_each) for route in weights}
    remaining = max(int(total_budget) - sum(budget.values()), 0)
    raw = {route: remaining * _float(weight, 0.0) for route, weight in weights.items()}
    for route, value in raw.items():
        budget[route] += int(value)
    leftovers = max(int(total_budget) - sum(budget.values()), 0)
    ranked = sorted(weights, key=lambda item: raw[item] - int(raw[item]), reverse=True)
    idx = 0
    while leftovers > 0 and ranked:
        budget[ranked[idx % len(ranked)]] += 1
        leftovers -= 1
        idx += 1
    return budget


def _merge_unique(preferred: Iterable[Any], current: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in list(preferred) + list(current):
        text = _text(value)
        if text and text not in out:
            out.append(text)
    return out


def _signal_value(strategy_signals: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in strategy_signals:
            return strategy_signals.get(key)
    return default


def load_scheduler_signal_context(config: Dict[str, Any]) -> Dict[str, Any]:
    paths = _dict(config.get("paths"))
    portfolio_root = Path(str(paths.get("portfolio_output_root", "") or "").strip())
    bridge_root = Path(str(paths.get("bridge_root", "") or "").strip())
    feedback_root = Path(str(paths.get("live_execution_root", "") or "").strip()) / "oms_v1" / "feedback"
    recommendation = _load_latest_json(portfolio_root / "portfolio_recommendation.json")
    return {
        "latest_portfolio_recommendation": recommendation,
        "latest_global_objective": _dict(recommendation.get("global_objective")),
        "latest_harvest_risk": _dict(recommendation.get("harvest_risk")),
        "latest_econometric_guardrails": _dict(recommendation.get("econometric_guardrails")),
        "latest_control_feedback": _load_latest_json(feedback_root / "control_feedback_latest.json"),
        "latest_research_meta_feedback": _load_latest_json(feedback_root / "research_meta_feedback_latest.json"),
        "bridge_performance_feedback": _load_latest_json(bridge_root / "performance_feedback.json"),
    }


def _build_signal_advisor_chain(
    *,
    profile: str,
    market_state: Dict[str, Any],
    strategy_signals: Dict[str, Any],
    previous_objective: Dict[str, Any],
    previous_guardrails: Dict[str, Any],
    previous_harvest: Dict[str, Any],
    research_meta_feedback: Dict[str, Any],
    control_feedback: Dict[str, Any],
    bridge_feedback: Dict[str, Any],
) -> List[Dict[str, Any]]:
    objective_scores = _dict(previous_objective.get("scores"))
    objective_flags = _list(previous_objective.get("hard_flags"))
    metrics = _dict(strategy_signals.get("metrics"))
    local_signal_trace = _list(strategy_signals.get("signal_trace"))
    route_signal_weights = _dict(strategy_signals.get("signal_route_weights"))
    research_signal = _dict(_signal_value(strategy_signals, "research_constraint_signals", "strategy_constraint_signals"))
    portfolio_signal = _dict(_signal_value(strategy_signals, "portfolio_constraint_signals", "portfolio_overrides"))
    advisor_chain = [
        build_advice(
            advisor="local_performance_signal",
            category="signal",
            status="ok" if bool(strategy_signals.get("available", False)) else "inactive",
            summary=_text(strategy_signals.get("regime")) or "neutral",
            reasons=[_text(item) for item in local_signal_trace if _text(item)],
            evidence={
                "profile": _text(profile),
                "daily_return": _float(metrics.get("daily_return"), 0.0),
                "three_day_return": _float(metrics.get("three_day_return"), 0.0),
                "five_day_return": _float(metrics.get("five_day_return"), 0.0),
                "current_drawdown": _float(metrics.get("current_drawdown"), 0.0),
                "route_signal_weights": route_signal_weights,
            },
            payload={
                "research_constraint_signals": research_signal,
                "portfolio_constraint_signals": portfolio_signal,
                "route_space_signals": _dict(strategy_signals.get("route_space_signals")),
                "preferred_model_signals": _list(strategy_signals.get("preferred_model_signals")),
                "ban_model_signals": _list(strategy_signals.get("ban_model_signals")),
            },
        ),
        build_advice(
            advisor="research_runtime_stability",
            category="risk",
            status="warning" if _text(profile).lower() == "quick_test" else "ok",
            summary="quick_test_prefers_stable_families" if _text(profile).lower() == "quick_test" else "full_route_space_allowed",
            reasons=["ban_xgboost_gpu_for_quick_test", "ban_generated_family_for_quick_test"] if _text(profile).lower() == "quick_test" else [],
            evidence={
                "profile": _text(profile),
                "quick_test": _text(profile).lower() == "quick_test",
            },
        ),
        build_advice(
            advisor="market_state_signal",
            category="signal",
            status="warning" if _text(market_state.get("new_position_policy")).lower() in {"reduce_only", "block"} else "ok",
            summary=_text(market_state.get("market_regime")) or "unknown",
            reasons=[
                _text(market_state.get("style_bias")),
                _text(market_state.get("mechanism_bias")),
                _text(market_state.get("new_position_policy")),
            ],
            evidence={
                "risk_budget_multiplier": _float(market_state.get("risk_budget_multiplier"), 1.0),
                "market_regime": _text(market_state.get("market_regime")),
                "style_bias": _text(market_state.get("style_bias")),
                "mechanism_bias": _text(market_state.get("mechanism_bias")),
            },
        ),
        build_advice(
            advisor="previous_global_objective",
            category="arbitration",
            status="warning" if objective_flags else "ok",
            summary=_text(previous_objective.get("policy_posture")) or "balanced",
            reasons=[_text(item) for item in objective_flags if _text(item)],
            evidence={
                "overall": _float(objective_scores.get("overall"), 0.0),
                "evidence": _float(objective_scores.get("evidence"), 0.0),
                "diversity": _float(objective_scores.get("diversity"), 0.0),
                "execution": _float(objective_scores.get("execution"), 0.0),
                "adversarial": _float(objective_scores.get("adversarial"), 0.0),
            },
            payload=previous_objective,
        ),
        build_advice(
            advisor="econometric_guardrails",
            category="risk",
            status="warning" if _float(previous_guardrails.get("guardrail_penalty"), 0.0) >= 0.60 else "ok",
            summary=_text(previous_guardrails.get("stability_flag")) or "unknown",
            reasons=[
                f"guardrail_penalty={_float(previous_guardrails.get('guardrail_penalty'), 0.0):.3f}",
                f"incremental_value={_float(previous_guardrails.get('incremental_value_score'), 0.0):.3f}",
            ],
            evidence={
                "guardrail_penalty": _float(previous_guardrails.get("guardrail_penalty"), 0.0),
                "stability_score": _float(previous_guardrails.get("stability_score"), 0.0),
                "regime_dependency_score": _float(previous_guardrails.get("regime_dependency_score"), 0.0),
            },
            payload=previous_guardrails,
        ),
        build_advice(
            advisor="harvest_risk",
            category="risk",
            status="warning" if _float(previous_harvest.get("harvest_risk_score"), 0.0) >= 0.55 else "ok",
            summary=_text(previous_harvest.get("harvest_risk_level")) or "unknown",
            reasons=[_text(item) for item in _list(previous_harvest.get("tags")) if _text(item)],
            evidence={
                "harvest_risk_score": _float(previous_harvest.get("harvest_risk_score"), 0.0),
                "dominant_family": _text(previous_harvest.get("dominant_family")),
                "dominant_family_share": _float(previous_harvest.get("dominant_family_share"), 0.0),
            },
            payload=previous_harvest,
        ),
        build_advice(
            advisor="execution_feedback",
            category="feedback",
            status="warning" if _text(research_meta_feedback.get("review_summary")) or _text(control_feedback.get("summary")) else "ok",
            summary=_text(research_meta_feedback.get("review_summary"))
            or _text(research_meta_feedback.get("summary"))
            or _text(control_feedback.get("summary"))
            or "feedback_quiet",
            reasons=[
                _text(item)
                for item in [
                    research_meta_feedback.get("review_summary"),
                    research_meta_feedback.get("summary"),
                    control_feedback.get("summary"),
                ]
                if _text(item)
            ],
            evidence={
                "research_meta_available": bool(research_meta_feedback),
                "control_feedback_available": bool(control_feedback),
                "bridge_feedback_available": bool(bridge_feedback),
            },
            payload={
                "research_meta_feedback": _compact_feedback_payload(research_meta_feedback),
                "control_feedback": _compact_feedback_payload(control_feedback),
                "bridge_feedback": _compact_feedback_payload(bridge_feedback),
            },
        ),
    ]
    return advisor_chain


def build_research_budget_decision(
    *,
    config: Dict[str, Any],
    profile: str,
    market_state: Dict[str, Any],
    strategy_signals: Dict[str, Any],
    signal_context: Dict[str, Any],
) -> Dict[str, Any]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    previous_objective = _dict(signal_context.get("latest_global_objective"))
    previous_guardrails = _dict(signal_context.get("latest_econometric_guardrails"))
    previous_harvest = _dict(signal_context.get("latest_harvest_risk"))
    research_meta_feedback = _dict(signal_context.get("latest_research_meta_feedback"))
    control_feedback = _dict(signal_context.get("latest_control_feedback"))
    bridge_feedback = _dict(signal_context.get("bridge_performance_feedback"))
    market_regime = _text(market_state.get("market_regime")) or "unknown"
    strategy_metrics = _dict(strategy_signals.get("metrics"))
    signal_route_weights = dict(strategy_signals.get("signal_route_weights", {}) or bridge_feedback.get("signal_route_weights", {}) or {})
    if not signal_route_weights:
        signal_route_weights = {"feature": 0.18, "model": 0.18, "training": 0.16, "portfolio": 0.16, "risk": 0.14, "data": 0.10, "hybrid": 0.08}

    research_signal = _dict(_signal_value(strategy_signals, "research_constraint_signals", "strategy_constraint_signals", "strategy_overrides"))
    portfolio_signal = _dict(_signal_value(strategy_signals, "portfolio_constraint_signals", "portfolio_overrides"))
    route_space_signals = _dict(strategy_signals.get("route_space_signals"))

    weights = {k: _float(v, 0.0) for k, v in signal_route_weights.items()}
    reasons: List[str] = [f"profile={profile}", f"market_regime={market_regime}"]
    objective_scores = _dict(previous_objective.get("scores"))
    objective_flags = set(_list(previous_objective.get("hard_flags")))
    guardrail_penalty = _float(previous_guardrails.get("guardrail_penalty"), 0.0)
    harvest_score = _float(previous_harvest.get("harvest_risk_score"), 0.0)
    drawdown = _float(strategy_metrics.get("current_drawdown"), 0.0)
    daily_return = _float(strategy_metrics.get("daily_return"), 0.0)
    three_day_return = _float(strategy_metrics.get("three_day_return"), 0.0)
    risk_budget_multiplier = _float(market_state.get("risk_budget_multiplier"), 1.0)

    if "evidence_below_floor" in objective_flags:
        _scale_route(weights, "data", 1.45)
        _scale_route(weights, "feature", 1.25)
        _scale_route(weights, "risk", 1.15)
        _scale_route(weights, "training", 0.85)
        reasons.append("boost_data_feature_for_weak_evidence")
    if "family_concentration_above_ceiling" in objective_flags or harvest_score >= 0.55:
        _scale_route(weights, "portfolio", 1.35)
        _scale_route(weights, "risk", 1.25)
        _scale_route(weights, "hybrid", 1.15)
        _scale_route(weights, "model", 0.85)
        reasons.append("diversify_against_family_crowding")
    if "guardrail_penalty_above_ceiling" in objective_flags or guardrail_penalty >= 0.60:
        _scale_route(weights, "risk", 1.40)
        _scale_route(weights, "data", 1.25)
        _scale_route(weights, "model", 0.75)
        _scale_route(weights, "training", 0.75)
        reasons.append("reduce_model_training_when_guardrails_fragile")
    if "incremental_value_below_floor" in objective_flags:
        _scale_route(weights, "data", 1.20)
        _scale_route(weights, "feature", 1.10)
        _scale_route(weights, "hybrid", 0.90)
        reasons.append("seek_new_information_sources")
    if "execution_below_floor" in objective_flags:
        _scale_route(weights, "portfolio", 1.40)
        _scale_route(weights, "risk", 1.25)
        _scale_route(weights, "model", 0.85)
        reasons.append("tilt_to_execution_and_risk_repairs")
    if risk_budget_multiplier <= 0.70:
        _scale_route(weights, "risk", 1.20)
        _scale_route(weights, "portfolio", 1.10)
        reasons.append("market_state_reduces_risk_budget")
    elif risk_budget_multiplier >= 1.10:
        _scale_route(weights, "model", 1.10)
        _scale_route(weights, "training", 1.08)
        reasons.append("market_state_supports_exploration")

    feedback_text = " ".join(
        [
            _text(research_meta_feedback.get("summary")),
            _text(control_feedback.get("summary")),
            _text(research_meta_feedback.get("review_summary")),
        ]
    ).lower()
    if "weak thesis" in feedback_text or "fallback" in feedback_text or "fact-backed" in feedback_text:
        _scale_route(weights, "data", 1.20)
        _scale_route(weights, "portfolio", 1.10)
        _scale_route(weights, "risk", 1.10)
        reasons.append("execution_feedback_requested_stronger_thesis")

    if drawdown <= -0.05 or three_day_return <= -0.03 or daily_return <= -0.02:
        _scale_route(weights, "risk", 1.25)
        _scale_route(weights, "portfolio", 1.10)
        reasons.append("defensive_due_to_recent_drawdown")
    elif _float(objective_scores.get("overall"), 0.0) >= 0.68 and guardrail_penalty <= 0.35:
        _scale_route(weights, "model", 1.15)
        _scale_route(weights, "training", 1.10)
        _scale_route(weights, "hybrid", 1.10)
        reasons.append("allow_more_model_training_when_objective_stable")

    normalized_weights = _normalize_weights(weights)
    base_cycles = _base_cycles_for_profile(profile)
    research_cfg = _dict(config.get("research_brain"))
    global_objective_cfg = _dict(config.get("global_objective"))
    rec_cfg = _dict(config.get("portfolio_recommendation"))
    base_budget = int(research_cfg.get("cycle_candidate_budget", 12) or 12)
    route_min_candidates = max(1, _int(research_cfg.get("route_min_candidates"), 1))
    max_cycles = base_cycles
    candidate_budget = base_budget
    final_verdict = "balanced"
    if guardrail_penalty >= 0.70 or harvest_score >= 0.70:
        max_cycles = max(1, base_cycles - 1)
        candidate_budget = max(8, base_budget - 2)
        final_verdict = "defensive"
        reasons.append("tighten_cycles_under_high_adversarial_risk")
    elif _float(objective_scores.get("overall"), 0.0) >= 0.72 and guardrail_penalty <= 0.30:
        max_cycles = min(base_cycles + 1, int(global_objective_cfg.get("max_cycles", max(base_cycles, 3)) or max(base_cycles, 3)))
        candidate_budget = min(base_budget + 2, 18)
        final_verdict = "expand"
        reasons.append("expand_budget_when_objective_stable")

    preferred_models = list(strategy_signals.get("preferred_model_signals", []) or bridge_feedback.get("preferred_model_signals", []) or [])
    ban_models = list(strategy_signals.get("ban_model_signals", []) or bridge_feedback.get("ban_model_signals", []) or [])
    if _text(profile).lower() == "quick_test":
        preferred_models = _merge_unique(["ridge_ranker", "lightgbm_gpu"], preferred_models)
        ban_models = _merge_unique(ban_models, ["xgboost_gpu", "generated_family"])
        reasons.append("quick_test_prefers_stable_model_families")
    if guardrail_penalty >= 0.60:
        ban_models = _merge_unique(ban_models, ["generated_family"])
        preferred_models = _merge_unique(["ridge_ranker", "lightgbm_gpu"], preferred_models)
    elif _float(objective_scores.get("overall"), 0.0) >= 0.65:
        preferred_models = _merge_unique(["xgboost_gpu", "lightgbm_gpu", "ridge_ranker"], preferred_models)

    route_space_overrides = dict(route_space_signals)
    if _text(profile).lower() == "quick_test":
        route_space_overrides["model_families"] = [model for model in _merge_unique(preferred_models, ["ridge_ranker", "lightgbm_gpu"]) if model not in set(ban_models)]
    if "data" in sorted(normalized_weights, key=lambda item: normalized_weights[item], reverse=True)[:2]:
        route_space_overrides.setdefault("feature_profiles", ["baseline_plus", "vol_liq_quality", "generated_feature_pack"])
    if guardrail_penalty >= 0.60:
        allowed_models = route_space_overrides.get("model_families", preferred_models or ["ridge_ranker", "lightgbm_gpu"])
        route_space_overrides["model_families"] = [m for m in allowed_models if _text(m) and _text(m) not in {"generated_family"}]

    research_brain_overrides = {
        "max_cycles": int(max_cycles),
        "cycle_candidate_budget": int(candidate_budget),
        "route_min_candidates": int(route_min_candidates),
    }

    strategy_overrides = {
        "top_k": int(research_signal.get("top_k", 20) or 20),
        "portfolio_base_exposure": round(_clip(_float(research_signal.get("portfolio_base_exposure", 1.0), 1.0), 0.10, 1.20), 4),
        "portfolio_weak_market_exposure": round(_clip(_float(research_signal.get("portfolio_weak_market_exposure", 0.5), 0.5), 0.05, 0.90), 4),
        "portfolio_single_name_cap": round(_clip(_float(research_signal.get("portfolio_single_name_cap", portfolio_signal.get("single_name_cap", 0.10)), 0.10), 0.02, 0.20), 4),
    }
    portfolio_overrides = {
        "max_names": int(portfolio_signal.get("max_names", rec_cfg.get("max_names", 20)) or 20),
        "single_name_cap": round(_clip(_float(portfolio_signal.get("single_name_cap", rec_cfg.get("single_name_cap", 0.10)), 0.10), 0.02, 0.20), 4),
        "total_exposure_cap": round(_clip(_float(portfolio_signal.get("total_exposure_cap", rec_cfg.get("total_exposure_cap", 1.0)), 1.0), 0.20, 1.20), 4),
    }
    if final_verdict == "defensive":
        strategy_overrides["portfolio_base_exposure"] = round(min(strategy_overrides["portfolio_base_exposure"], 0.78), 4)
        strategy_overrides["portfolio_weak_market_exposure"] = round(min(strategy_overrides["portfolio_weak_market_exposure"], 0.28), 4)
        strategy_overrides["portfolio_single_name_cap"] = round(min(strategy_overrides["portfolio_single_name_cap"], 0.08), 4)
        portfolio_overrides["single_name_cap"] = round(min(portfolio_overrides["single_name_cap"], 0.08), 4)
        portfolio_overrides["total_exposure_cap"] = round(min(portfolio_overrides["total_exposure_cap"], 0.85), 4)
        portfolio_overrides["max_names"] = min(int(portfolio_overrides["max_names"]), 14)
    elif final_verdict == "expand":
        strategy_overrides["portfolio_base_exposure"] = round(max(strategy_overrides["portfolio_base_exposure"], 0.95), 4)
        strategy_overrides["portfolio_weak_market_exposure"] = round(max(strategy_overrides["portfolio_weak_market_exposure"], 0.45), 4)
        portfolio_overrides["total_exposure_cap"] = round(max(portfolio_overrides["total_exposure_cap"], 0.95), 4)
        portfolio_overrides["max_names"] = max(int(portfolio_overrides["max_names"]), 18)

    route_budget = _allocate_route_budget(normalized_weights, candidate_budget, route_min_candidates)
    scheduler_identity = artifact_identity(
        run_id=_text(_dict(signal_context.get("latest_portfolio_recommendation")).get("run_id")),
        trade_date=_text(_dict(_dict(signal_context.get("latest_portfolio_recommendation")).get("artifact_identity")).get("trade_date")),
        release_id="",
        phase="research_scheduler",
        producer="objective_scheduler",
    )
    advisor_chain = _build_signal_advisor_chain(
        profile=profile,
        market_state=market_state,
        strategy_signals=strategy_signals,
        previous_objective=previous_objective,
        previous_guardrails=previous_guardrails,
        previous_harvest=previous_harvest,
        research_meta_feedback=research_meta_feedback,
        control_feedback=control_feedback,
        bridge_feedback=bridge_feedback,
    )
    return {
        "version": "research_scheduler_decision_v2",
        "generated_at": now_text,
        "authority_owner": "intelligent_scheduler",
        "artifact_identity": scheduler_identity,
        "status": "ready",
        "final_verdict": final_verdict,
        "policy_posture": _text(previous_objective.get("policy_posture")) or "balanced",
        "reason_chain": compact_reason_chain(advisor_chain) + reasons,
        "advisor_chain": advisor_chain,
        "signal_snapshot": {
            "strategy_signals": strategy_signals,
            "market_state": market_state,
            "previous_global_objective": previous_objective,
            "previous_harvest_risk": previous_harvest,
            "previous_econometric_guardrails": previous_guardrails,
            "research_meta_feedback": _compact_feedback_payload(research_meta_feedback),
            "control_feedback": _compact_feedback_payload(control_feedback),
            "bridge_feedback": _compact_feedback_payload(bridge_feedback),
        },
        "research_plan": {
            "base_profile_cycles": int(base_cycles),
            "route_weights": normalized_weights,
            "route_budget": route_budget,
            "route_space_overrides": route_space_overrides,
            "preferred_model_families": preferred_models,
            "ban_model_families": ban_models,
            "research_brain_overrides": research_brain_overrides,
        },
        "portfolio_construction": {
            "strategy_overrides": strategy_overrides,
            "portfolio_overrides": portfolio_overrides,
        },
        "route_weights": normalized_weights,
        "route_budget": route_budget,
        "route_space_overrides": route_space_overrides,
        "preferred_model_families": preferred_models,
        "ban_model_families": ban_models,
        "research_brain_overrides": research_brain_overrides,
        "strategy_overrides": strategy_overrides,
        "portfolio_overrides": portfolio_overrides,
        "reasons": reasons,
    }


def merge_signals_with_budget_feedback(
    *,
    strategy_signals: Dict[str, Any],
    budget_decision: Dict[str, Any],
) -> Dict[str, Any]:
    payload = dict(strategy_signals or {})
    compact_decision = _compact_budget_decision(budget_decision)
    payload["route_weights"] = dict(budget_decision.get("route_weights", {}) or {})
    payload["route_budget"] = dict(budget_decision.get("route_budget", {}) or {})
    payload["route_space_overrides"] = dict(budget_decision.get("route_space_overrides", {}) or {})
    payload["preferred_model_families"] = list(budget_decision.get("preferred_model_families", []) or [])
    payload["ban_model_families"] = list(budget_decision.get("ban_model_families", []) or [])
    payload["research_brain_overrides"] = dict(budget_decision.get("research_brain_overrides", {}) or {})
    payload["strategy_overrides"] = dict(budget_decision.get("strategy_overrides", {}) or {})
    payload["portfolio_overrides"] = dict(budget_decision.get("portfolio_overrides", {}) or {})
    payload["scheduler_budget_decision"] = compact_decision
    payload["scheduler_research_decision"] = compact_decision
    return payload
