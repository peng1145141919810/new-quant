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


def _ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator <= 0:
        return float(default)
    return float(numerator) / float(denominator)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _risk_scale(text: str) -> float:
    value = _text(text).lower()
    if value in {"low", "green"}:
        return 0.15
    if value in {"medium", "amber", "guarded"}:
        return 0.45
    if value in {"high", "red"}:
        return 0.80
    if value in {"critical", "halt", "blocked"}:
        return 1.0
    return 0.35


def assess_harvest_risk(
    *,
    source_summary: Dict[str, Any],
    market_state: Dict[str, Any],
    execution_review: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    summary = _dict(source_summary)
    candidate_pool_stats = _dict(summary.get("candidate_pool_stats"))
    llm_candidate_review = _dict(summary.get("candidate_pool_llm_review"))
    llm_candidate_payload = _dict(llm_candidate_review.get("review"))
    review_payload = _dict(_dict(execution_review).get("review"))
    alpha_family_counts = _dict(candidate_pool_stats.get("alpha_family_counts"))
    total_candidates = _float(candidate_pool_stats.get("candidate_count"), _float(summary.get("n_names"), 0.0))
    fact_backed = _float(candidate_pool_stats.get("fact_backed_candidates"), 0.0)
    fallback_count = _float(candidate_pool_stats.get("fallback_source_count"), 0.0)
    family_total = sum(_float(v, 0.0) for v in alpha_family_counts.values())
    dominant_family = ""
    dominant_share = 0.0
    if family_total > 0:
        dominant_family, dominant_value = max(alpha_family_counts.items(), key=lambda item: _float(item[1], 0.0))
        dominant_share = _float(dominant_value, 0.0) / family_total

    turnover_multiplier = _float(market_state.get("turnover_multiplier"), 1.0)
    risk_flags = list(llm_candidate_payload.get("risk_flags", []) or [])
    review_flags = list(review_payload.get("risk_flags", []) or [])
    tags: List[str] = []

    family_penalty = _clamp((dominant_share - 0.42) / 0.45) if dominant_share > 0 else 0.0
    fallback_penalty = _ratio(fallback_count, total_candidates)
    evidence_gap = 1.0 - _ratio(fact_backed, total_candidates, default=0.0)
    turnover_heat = _clamp(abs(turnover_multiplier - 1.0))
    llm_risk = _risk_scale(_text(review_payload.get("risk_level")) or _text(llm_candidate_payload.get("risk_level")))
    reason_weight = _clamp((len(risk_flags) + len(review_flags)) / 8.0)

    if dominant_share >= 0.65:
        tags.append("family_crowding")
    if fallback_penalty >= 0.35:
        tags.append("fallback_source_heavy")
    if evidence_gap >= 0.65:
        tags.append("weak_fact_coverage")
    if turnover_heat >= 0.35:
        tags.append("turnover_heat")
    if llm_risk >= 0.8:
        tags.append("execution_review_high_risk")

    score = _clamp(
        0.30 * family_penalty
        + 0.20 * fallback_penalty
        + 0.20 * evidence_gap
        + 0.15 * turnover_heat
        + 0.10 * llm_risk
        + 0.05 * reason_weight
    )
    confidence = _clamp(
        0.30
        + 0.20 * _clamp(total_candidates / 20.0)
        + 0.20 * _clamp(len(alpha_family_counts) / 5.0)
        + 0.15 * _clamp((len(risk_flags) + len(review_flags)) / 6.0)
        + 0.15 * _clamp(family_total / max(total_candidates, 1.0))
    )
    level = "low"
    if score >= 0.75:
        level = "critical"
    elif score >= 0.55:
        level = "high"
    elif score >= 0.35:
        level = "medium"

    return {
        "version": "harvest_risk_v1",
        "harvest_risk_score": score,
        "harvest_risk_level": level,
        "harvest_risk_confidence": confidence,
        "dominant_family": _text(dominant_family),
        "dominant_family_share": dominant_share,
        "tags": tags,
        "signals": {
            "candidate_count": int(total_candidates),
            "fact_backed_ratio": _ratio(fact_backed, total_candidates, default=0.0),
            "fallback_ratio": fallback_penalty,
            "turnover_heat": turnover_heat,
            "llm_risk": llm_risk,
            "reason_weight": reason_weight,
        },
        "reasons": (risk_flags + review_flags)[:8],
    }
