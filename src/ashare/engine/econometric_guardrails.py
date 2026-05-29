from __future__ import annotations

from typing import Any, Dict


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


def assess_econometric_guardrails(
    *,
    source_summary: Dict[str, Any],
    market_state: Dict[str, Any],
) -> Dict[str, Any]:
    summary = _dict(source_summary)
    candidate_pool_stats = _dict(summary.get("candidate_pool_stats"))
    alpha_family_counts = _dict(candidate_pool_stats.get("alpha_family_counts"))
    total_score = _float(summary.get("total_score"), 0.0)
    sharpe = _float(summary.get("sharpe"), 0.0)
    max_drawdown = abs(_float(summary.get("max_drawdown"), 0.0))
    candidate_count = _float(candidate_pool_stats.get("candidate_count"), _float(summary.get("n_names"), 0.0))
    fact_backed = _float(candidate_pool_stats.get("fact_backed_candidates"), 0.0)
    accepted_thesis = _float(candidate_pool_stats.get("accepted_thesis_count"), 0.0)
    fallback_count = _float(candidate_pool_stats.get("fallback_source_count"), 0.0)
    family_count = max(_float(candidate_pool_stats.get("family_count"), len(alpha_family_counts)), 0.0)

    total_family = sum(_float(v, 0.0) for v in alpha_family_counts.values())
    dominant_share = 0.0
    if total_family > 0:
        dominant_share = max(_float(v, 0.0) for v in alpha_family_counts.values()) / total_family

    fact_ratio = _ratio(fact_backed, candidate_count, default=0.0)
    thesis_ratio = _ratio(accepted_thesis, candidate_count, default=0.0)
    fallback_ratio = _ratio(fallback_count, candidate_count, default=0.0)

    sample_fragility = _clamp(
        0.50 * (1.0 - _clamp(candidate_count / 20.0))
        + 0.20 * _clamp(max_drawdown / 0.35)
        + 0.15 * _clamp((0.30 - fact_ratio) / 0.30)
        + 0.15 * _clamp((0.35 - thesis_ratio) / 0.35)
    )
    regime_dependency = _clamp(
        0.45 * _clamp((dominant_share - 0.45) / 0.55)
        + 0.20 * (1.0 - _clamp(family_count / 5.0))
        + 0.20 * _clamp(abs(_float(market_state.get("turnover_multiplier"), 1.0) - 1.0))
        + 0.15 * (0.75 if _text(market_state.get("style_bias")).lower() not in {"", "balanced"} else 0.25)
    )
    spurious_correlation_risk = _clamp(
        0.35 * _clamp((sharpe - 1.1) / 1.4)
        + 0.25 * (1.0 - fact_ratio)
        + 0.20 * fallback_ratio
        + 0.20 * _clamp((dominant_share - 0.50) / 0.50)
    )
    incremental_value_score = _clamp(
        0.35 * fact_ratio
        + 0.25 * thesis_ratio
        + 0.20 * _clamp(family_count / 5.0)
        + 0.20 * (1.0 - fallback_ratio)
    )
    stability_score = _clamp(
        0.35 * _clamp((total_score - 35.0) / 45.0)
        + 0.25 * _clamp((sharpe + 0.2) / 1.8)
        + 0.20 * (1.0 - _clamp(max_drawdown / 0.35))
        + 0.20 * (1.0 - sample_fragility)
    )
    guardrail_penalty = _clamp(
        0.35 * sample_fragility
        + 0.35 * regime_dependency
        + 0.30 * spurious_correlation_risk
    )
    stability_flag = "stable"
    if guardrail_penalty >= 0.72 or stability_score <= 0.32:
        stability_flag = "fragile"
    elif guardrail_penalty >= 0.48 or stability_score <= 0.50:
        stability_flag = "warning"

    return {
        "version": "econometric_guardrails_v1",
        "stability_flag": stability_flag,
        "stability_score": stability_score,
        "sample_fragility_score": sample_fragility,
        "regime_dependency_score": regime_dependency,
        "spurious_correlation_risk": spurious_correlation_risk,
        "incremental_value_score": incremental_value_score,
        "guardrail_penalty": guardrail_penalty,
        "signals": {
            "candidate_count": int(candidate_count),
            "fact_backed_ratio": fact_ratio,
            "accepted_thesis_ratio": thesis_ratio,
            "fallback_ratio": fallback_ratio,
            "dominant_family_share": dominant_share,
            "family_count": int(family_count),
        },
    }
