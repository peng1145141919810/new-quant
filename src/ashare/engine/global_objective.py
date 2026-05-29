from __future__ import annotations

from typing import Any, Dict

from .econometric_guardrails import assess_econometric_guardrails
from .harvest_risk import assess_harvest_risk


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


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator <= 0:
        return float(default)
    return float(numerator) / float(denominator)


def _risk_scale(text: str) -> float:
    value = _text(text).lower()
    if value in {"low", "green"}:
        return 0.15
    if value in {"medium", "amber", "guarded"}:
        return 0.45
    if value in {"high", "red"}:
        return 0.8
    if value in {"critical", "halt", "blocked"}:
        return 1.0
    return 0.35


def _concentration_penalty(counts: Dict[str, Any]) -> float:
    normalized = {str(k): _float(v) for k, v in dict(counts or {}).items() if _float(v) > 0}
    total = sum(normalized.values())
    if total <= 0:
        return 0.0
    max_share = max(normalized.values()) / total
    return _clamp((max_share - 0.45) / 0.55)


def _constitution(config: Dict[str, Any]) -> Dict[str, Any]:
    root = _dict(config.get("global_objective"))
    return {
        "minimum_evidence_score": _float(root.get("minimum_evidence_score"), 0.35),
        "maximum_harvest_risk": _float(root.get("maximum_harvest_risk"), 0.85),
        "maximum_family_concentration": _float(root.get("maximum_family_concentration"), 0.60),
        "minimum_execution_score": _float(root.get("minimum_execution_score"), 0.30),
        "minimum_candidate_count": _int(root.get("minimum_candidate_count"), 3),
        "maximum_guardrail_penalty": _float(root.get("maximum_guardrail_penalty"), 0.78),
        "minimum_incremental_value_score": _float(root.get("minimum_incremental_value_score"), 0.28),
    }


def _policy(config: Dict[str, Any]) -> Dict[str, Any]:
    root = _dict(config.get("global_objective"))
    return {
        "outcome_weight": _float(root.get("outcome_weight"), 0.22),
        "evidence_weight": _float(root.get("evidence_weight"), 0.24),
        "diversity_weight": _float(root.get("diversity_weight"), 0.18),
        "execution_weight": _float(root.get("execution_weight"), 0.18),
        "adversarial_weight": _float(root.get("adversarial_weight"), 0.18),
        "guardrail_weight": _float(root.get("guardrail_weight"), 0.12),
        "exploration_budget": _float(root.get("exploration_budget"), 0.15),
        "max_cycles": _int(root.get("max_cycles"), 3),
    }


def build_global_objective_snapshot(
    *,
    config: Dict[str, Any],
    stage: str,
    source_summary: Dict[str, Any],
    market_state: Dict[str, Any],
    harvest_risk: Dict[str, Any] | None = None,
    econometric_guardrails: Dict[str, Any] | None = None,
    execution_review: Dict[str, Any] | None = None,
    account_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    summary = _dict(source_summary)
    candidate_pool_stats = _dict(summary.get("candidate_pool_stats"))
    alpha_family_counts = _dict(candidate_pool_stats.get("alpha_family_counts"))
    llm_candidate_review = _dict(summary.get("candidate_pool_llm_review"))
    llm_candidate_payload = _dict(llm_candidate_review.get("review"))
    technical_confirmation = _dict(summary.get("technical_confirmation"))
    trade_discipline = _dict(summary.get("trade_discipline"))
    portfolio_posture = _dict(summary.get("portfolio_posture"))
    review_payload = _dict(_dict(execution_review).get("review"))
    harvest = _dict(harvest_risk)
    guardrails = _dict(econometric_guardrails)
    constitution = _constitution(config)
    policy = _policy(config)

    total_score = _float(summary.get("total_score"), 0.0)
    sharpe = _float(summary.get("sharpe"), 0.0)
    max_drawdown = abs(_float(summary.get("max_drawdown"), 0.0))
    outcome_score = _clamp(
        0.45 * _clamp(total_score / 100.0)
        + 0.35 * _clamp((sharpe + 1.0) / 3.0)
        + 0.20 * (1.0 - _clamp(max_drawdown / 0.35))
    )

    candidate_count = _float(candidate_pool_stats.get("candidate_count"), _float(summary.get("n_names"), 0.0))
    fact_backed = _float(candidate_pool_stats.get("fact_backed_candidates"), 0.0)
    accepted_thesis = _float(candidate_pool_stats.get("accepted_thesis_count"), 0.0)
    fallback_count = _float(candidate_pool_stats.get("fallback_source_count"), 0.0)
    fact_backed_ratio = _ratio(fact_backed, candidate_count)
    thesis_ratio = _ratio(accepted_thesis, candidate_count)
    fallback_ratio = _ratio(fallback_count, candidate_count)
    evidence_score = _clamp(
        0.45 * fact_backed_ratio
        + 0.25 * thesis_ratio
        + 0.15 * (1.0 - fallback_ratio)
        + 0.15 * (1.0 - _risk_scale(_text(llm_candidate_payload.get("risk_level"))))
    )

    concentration_penalty = _concentration_penalty(alpha_family_counts)
    diversity_score = _clamp(
        0.60 * (1.0 - concentration_penalty)
        + 0.20 * _clamp(_float(candidate_pool_stats.get("family_count"), len(alpha_family_counts)) / 5.0)
        + 0.20 * _clamp(_float(candidate_pool_stats.get("candidate_count"), 0.0) / 20.0)
    )

    posture = _text(trade_discipline.get("posture")).lower()
    market_turnover_multiplier = _float(market_state.get("turnover_multiplier"), 1.0)
    technical_allow_ratio = _ratio(
        _float(technical_confirmation.get("allow_count"), 0.0),
        _float(technical_confirmation.get("total_count"), _float(summary.get("n_names"), 0.0)),
        default=0.5,
    )
    execution_risk = _risk_scale(_text(review_payload.get("risk_level")) or _text(portfolio_posture.get("risk_level")))
    execution_score = _clamp(
        0.40 * technical_allow_ratio
        + 0.30 * (1.0 - execution_risk)
        + 0.20 * (1.0 - _clamp(abs(market_turnover_multiplier - 1.0)))
        + 0.10 * (0.35 if posture in {"defensive", "reduce_only"} else 0.80)
    )

    harvest_score = _clamp(1.0 - _float(harvest.get("harvest_risk_score"), 0.0))
    guardrail_score = _clamp(1.0 - _float(guardrails.get("guardrail_penalty"), 0.0))
    overall_score = _clamp(
        policy["outcome_weight"] * outcome_score
        + policy["evidence_weight"] * evidence_score
        + policy["diversity_weight"] * diversity_score
        + policy["execution_weight"] * execution_score
        + policy["adversarial_weight"] * harvest_score
        + policy["guardrail_weight"] * guardrail_score
    )

    policy_posture = "balanced"
    if overall_score >= 0.72 and evidence_score >= constitution["minimum_evidence_score"]:
        policy_posture = "aggressive"
    elif harvest_score < 0.35 or execution_score < constitution["minimum_execution_score"]:
        policy_posture = "defensive"

    hard_flags = []
    if evidence_score < constitution["minimum_evidence_score"]:
        hard_flags.append("evidence_below_floor")
    if _float(harvest.get("harvest_risk_score"), 0.0) > constitution["maximum_harvest_risk"]:
        hard_flags.append("harvest_risk_above_ceiling")
    if concentration_penalty > constitution["maximum_family_concentration"]:
        hard_flags.append("family_concentration_above_ceiling")
    if execution_score < constitution["minimum_execution_score"]:
        hard_flags.append("execution_below_floor")
    if candidate_count < constitution["minimum_candidate_count"]:
        hard_flags.append("candidate_count_below_floor")
    if _float(guardrails.get("guardrail_penalty"), 0.0) > constitution["maximum_guardrail_penalty"]:
        hard_flags.append("guardrail_penalty_above_ceiling")
    if _float(guardrails.get("incremental_value_score"), 0.0) < constitution["minimum_incremental_value_score"]:
        hard_flags.append("incremental_value_below_floor")

    account_ctx = _dict(account_snapshot)
    return {
        "version": "global_objective_v1",
        "stage": _text(stage) or "unknown",
        "constitution": constitution,
        "policy": policy,
        "scores": {
            "outcome": outcome_score,
            "evidence": evidence_score,
            "diversity": diversity_score,
            "execution": execution_score,
            "adversarial": harvest_score,
            "guardrail": guardrail_score,
            "overall": overall_score,
        },
        "signals": {
            "candidate_count": int(candidate_count),
            "fact_backed_ratio": fact_backed_ratio,
            "accepted_thesis_ratio": thesis_ratio,
            "fallback_ratio": fallback_ratio,
            "family_concentration_penalty": concentration_penalty,
            "technical_allow_ratio": technical_allow_ratio,
            "execution_risk_level": _text(review_payload.get("risk_level")) or _text(portfolio_posture.get("risk_level")),
            "market_regime": _text(market_state.get("market_regime")),
            "style_bias": _text(market_state.get("style_bias")),
            "account_nav": _float(account_ctx.get("nav"), 0.0),
            "guardrail_penalty": _float(guardrails.get("guardrail_penalty"), 0.0),
            "incremental_value_score": _float(guardrails.get("incremental_value_score"), 0.0),
        },
        "hard_flags": hard_flags,
        "policy_posture": policy_posture,
        "recommended_budget": {
            "exploration_budget": policy["exploration_budget"],
            "max_cycles": policy["max_cycles"],
            "early_stop": bool(
                overall_score < 0.40
                or (
                    "guardrail_penalty_above_ceiling" in hard_flags
                    and candidate_count >= constitution["minimum_candidate_count"]
                )
            ),
            "shadow_only": bool(
                "execution_below_floor" in hard_flags
                or "harvest_risk_above_ceiling" in hard_flags
                or "guardrail_penalty_above_ceiling" in hard_flags
            ),
        },
    }


def build_unified_objective_bundle(
    *,
    config: Dict[str, Any],
    stage: str,
    source_summary: Dict[str, Any],
    market_state: Dict[str, Any],
    execution_review: Dict[str, Any] | None = None,
    account_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    harvest_risk = assess_harvest_risk(
        source_summary=source_summary,
        market_state=market_state,
        execution_review=execution_review,
    )
    econometric_guardrails = assess_econometric_guardrails(
        source_summary=source_summary,
        market_state=market_state,
    )
    global_objective = build_global_objective_snapshot(
        config=config,
        stage=stage,
        source_summary=source_summary,
        market_state=market_state,
        harvest_risk=harvest_risk,
        econometric_guardrails=econometric_guardrails,
        execution_review=execution_review,
        account_snapshot=account_snapshot,
    )
    return {
        "version": "unified_objective_bundle_v1",
        "harvest_risk": harvest_risk,
        "econometric_guardrails": econometric_guardrails,
        "global_objective": global_objective,
    }
