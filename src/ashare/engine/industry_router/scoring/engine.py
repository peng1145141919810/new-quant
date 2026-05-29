from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..core.common import clip, freshness_weight, mean_or_zero, safe_float
from ..schemas import CompanyExposure, EvidenceBundle, IndustryStrategySpec, ShockTypeSpec, ThesisScoreCard


def _shock_spec(strategy_spec: IndustryStrategySpec, shock_type: str) -> ShockTypeSpec:
    return strategy_spec.shock_types.get(shock_type) or ShockTypeSpec(
        shock_type=shock_type or "demand_expansion",
        half_life_days=20,
        min_persistence_days=10,
        invalidate_conditions=[],
    )


def _top_exposures(exposures: List[CompanyExposure]) -> List[CompanyExposure]:
    return sorted(
        [item for item in exposures if item.active_flag],
        key=lambda item: (
            item.exposure_strength * 0.45
            + item.purity_score * 0.35
            + item.mapping_confidence * 0.20
        ),
        reverse=True,
    )[:3]


def build_thesis_score(
    bundle: EvidenceBundle,
    exposures: List[CompanyExposure],
    price_context: Dict[str, Dict[str, Any]],
    strategy_spec: IndustryStrategySpec,
    shock_type: str,
    as_of_date: str,
) -> Tuple[ThesisScoreCard, Dict[str, Any], bool, str]:
    thesis_policy = dict(strategy_spec.thesis_policy or {})
    score_weights = dict(strategy_spec.scoring_weights or {})
    shock = _shock_spec(strategy_spec=strategy_spec, shock_type=shock_type)
    positive_support = 0.0
    negative_support = 0.0
    total_support = 0.0
    event_support = 0.0
    freshness_scores: List[float] = []
    non_event_types: List[str] = []
    for item in bundle.items:
        signed_value = item.signed_value()
        magnitude = clip(abs(signed_value), 0.0, 1.0)
        contribution = clip(item.weight, 0.0, 1.0) * clip(item.confidence, 0.0, 1.0) * magnitude
        total_support += contribution
        if item.evidence_type == "event_clue":
            event_support += contribution
        else:
            if item.evidence_type not in non_event_types:
                non_event_types.append(item.evidence_type)
        if signed_value >= 0:
            positive_support += contribution
        else:
            negative_support += contribution
        freshness_scores.append(freshness_weight(item.date, as_of_date))

    event_share = event_support / total_support if total_support > 0 else 0.0
    evidence_score = clip(0.45 + positive_support - negative_support * 1.10, 0.0, 1.0)
    max_event_share = safe_float(thesis_policy.get("max_event_clue_share"), 0.45)
    if event_share > max_event_share:
        evidence_score = clip(evidence_score - (event_share - max_event_share) * 0.60, 0.0, 1.0)

    top_exposure_rows = _top_exposures(exposures)
    avg_purity = mean_or_zero([item.purity_score for item in top_exposure_rows])
    avg_mapping = mean_or_zero([item.mapping_confidence for item in top_exposure_rows])
    causal_clarity_score = clip(
        0.18
        + avg_purity * 0.38
        + avg_mapping * 0.22
        + min(len(non_event_types), 3) / 3.0 * 0.17
        + (0.10 if bundle.non_event_count > 0 else -0.12),
        0.0,
        1.0,
    )

    freshness_avg = mean_or_zero(freshness_scores) if freshness_scores else 0.35
    half_life_score = clip(float(shock.half_life_days) / 35.0, 0.25, 1.0)
    persistence_score = clip(
        freshness_avg * 0.55
        + min(len(bundle.evidence_types), 4) / 4.0 * 0.20
        + half_life_score * 0.25,
        0.0,
        1.0,
    )

    exposure_score = clip(
        mean_or_zero(
            [
                item.exposure_strength * 0.60
                + item.purity_score * 0.25
                + item.mapping_confidence * 0.15
                for item in top_exposure_rows
            ]
        ),
        0.0,
        1.0,
    )

    price_rows = [
        price_context.get(item.ts_code, {})
        for item in top_exposure_rows
        if price_context.get(item.ts_code)
        and (
            safe_float(dict(price_context.get(item.ts_code, {})).get("latest_close"), 0.0) > 0
            or dict(price_context.get(item.ts_code, {})).get("price_date")
        )
    ]
    underpricing_score = clip(
        mean_or_zero([safe_float(item.get("underpricing_score"), 0.0) for item in price_rows]) if price_rows else 0.5,
        0.0,
        1.0,
    )
    crowding_penalty = clip(
        mean_or_zero([safe_float(item.get("crowding_penalty"), 0.0) for item in price_rows]) if price_rows else 0.15,
        0.0,
        1.0,
    )

    final_score = clip(
        evidence_score * safe_float(score_weights.get("evidence_score"), 0.27)
        + causal_clarity_score * safe_float(score_weights.get("causal_clarity_score"), 0.16)
        + persistence_score * safe_float(score_weights.get("persistence_score"), 0.16)
        + exposure_score * safe_float(score_weights.get("exposure_score"), 0.18)
        + underpricing_score * safe_float(score_weights.get("underpricing_score"), 0.15)
        - crowding_penalty * safe_float(score_weights.get("crowding_penalty"), 0.08),
        0.0,
        1.0,
    )

    has_min_evidence = (
        bundle.evidence_count >= int(thesis_policy.get("min_total_evidence", 2) or 2)
        and bundle.non_event_count >= int(thesis_policy.get("min_non_event_evidence", 1) or 1)
        and event_share <= max_event_share + 0.05
    )
    allow_entry = (
        has_min_evidence
        and evidence_score >= safe_float(thesis_policy.get("entry_evidence_floor"), 0.55)
        and final_score >= safe_float(thesis_policy.get("entry_score"), 0.63)
    )
    negative_stop = safe_float(thesis_policy.get("negative_evidence_stop"), 0.35)
    if not has_min_evidence or evidence_score < negative_stop:
        state = "blocked"
    elif allow_entry:
        state = "entry"
    elif final_score >= safe_float(thesis_policy.get("hold_score"), 0.52):
        state = "hold"
    else:
        state = "watch"

    audit = {
        "evidence_count": bundle.evidence_count,
        "non_event_evidence_count": bundle.non_event_count,
        "event_evidence_count": bundle.event_count,
        "event_share": round(event_share, 4),
        "positive_support": round(positive_support, 4),
        "negative_support": round(negative_support, 4),
        "freshness_avg": round(freshness_avg, 4),
        "top_exposure_symbols": [item.ts_code for item in top_exposure_rows],
    }
    score_card = ThesisScoreCard(
        evidence_score=round(evidence_score, 4),
        causal_clarity_score=round(causal_clarity_score, 4),
        persistence_score=round(persistence_score, 4),
        exposure_score=round(exposure_score, 4),
        underpricing_score=round(underpricing_score, 4),
        crowding_penalty=round(crowding_penalty, 4),
        final_score=round(final_score, 4),
    )
    return score_card, audit, allow_entry, state
