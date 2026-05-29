from __future__ import annotations

from typing import Any, Dict, List

from ..core.common import clip, safe_float, safe_json_text, safe_text
from ..registry import ThemeRegistryRuntime
from ..scoring import build_thesis_score
from ..schemas import EvidenceBundle, IndustryStrategySpec, IndustryThesis, StockSignal


def _thesis_title(theme_name: str, shock_type: str) -> str:
    mapping = {
        "supply_cut": "供给收缩 thesis",
        "demand_expansion": "需求扩张 thesis",
        "inventory_reversal": "库存反转 thesis",
        "price_transmission": "价格传导 thesis",
        "policy_shift": "政策切换 thesis",
    }
    return f"{theme_name} {mapping.get(shock_type, '产业 thesis')}"


def build_theses(
    registry: ThemeRegistryRuntime,
    bundles: Dict[str, EvidenceBundle],
    price_context: Dict[str, Dict[str, Any]],
    strategy_spec: IndustryStrategySpec,
    as_of_date: str,
) -> List[IndustryThesis]:
    theses: List[IndustryThesis] = []
    for theme in registry.themes:
        exposures = registry.exposures_by_theme.get(theme.theme_id, [])
        bundle = bundles.get(theme.theme_id, EvidenceBundle(theme_id=theme.theme_id))
        score_card, audit, allow_entry, state = build_thesis_score(
            bundle=bundle,
            exposures=exposures,
            price_context=price_context,
            strategy_spec=strategy_spec,
            shock_type=theme.default_shock_type,
            as_of_date=as_of_date,
        )
        affected_chain_nodes = [item.chain_position for item in exposures if item.chain_position]
        beneficiary_symbols = [item.ts_code for item in exposures if item.benefit_direction == "long"]
        theses.append(
            IndustryThesis(
                thesis_id=f"{theme.theme_id}_{as_of_date.replace('-', '')}",
                theme_id=theme.theme_id,
                theme_name=theme.theme_name,
                mechanism_primary=theme.mechanism_primary,
                shock_type=theme.default_shock_type,
                title=_thesis_title(theme.theme_name, theme.default_shock_type),
                start_date=as_of_date,
                state=state,
                evidence_bundle=bundle,
                persistence_assessment=f"freshness={audit.get('freshness_avg', 0.0):.2f}; evidence_types={len(bundle.evidence_types)}",
                crowding_assessment=f"crowding_penalty={score_card.crowding_penalty:.2f}",
                affected_chain_nodes=list(dict.fromkeys(affected_chain_nodes)),
                beneficiary_symbols=list(dict.fromkeys(beneficiary_symbols)),
                invalidate_conditions=list(
                    getattr(strategy_spec.shock_types.get(theme.default_shock_type), "invalidate_conditions", []) or []
                ),
                score_card=score_card,
                allow_entry=allow_entry,
                audit=audit,
            )
        )
    return sorted(theses, key=lambda item: item.score_card.final_score, reverse=True)


def build_stock_signals(
    theses: List[IndustryThesis],
    registry: ThemeRegistryRuntime,
    price_context: Dict[str, Dict[str, Any]],
    event_clue_rows: List[Dict[str, Any]],
    strategy_spec: IndustryStrategySpec,
    as_of_date: str,
) -> List[StockSignal]:
    stock_policy = dict(strategy_spec.stock_signal_policy or {})
    clue_scores: Dict[tuple[str, str], List[float]] = {}
    clue_counts: Dict[tuple[str, str], int] = {}
    for row in event_clue_rows:
        theme_id = safe_text(row.get("theme_id"))
        symbol = safe_text(row.get("matched_symbol"))
        key = (theme_id, symbol)
        clue_scores.setdefault(key, []).append(safe_float(row.get("clue_strength"), 0.0))
        clue_counts[key] = clue_counts.get(key, 0) + 1
        theme_only_key = (theme_id, "")
        clue_scores.setdefault(theme_only_key, []).append(safe_float(row.get("clue_strength"), 0.0) * 0.6)
        clue_counts[theme_only_key] = clue_counts.get(theme_only_key, 0) + 1

    thesis_lookup = {item.theme_id: item for item in theses}
    signals: List[StockSignal] = []
    for exposure in registry.exposures:
        thesis = thesis_lookup.get(exposure.theme_id)
        if thesis is None:
            continue
        price_row = dict(price_context.get(exposure.ts_code, {}) or {})
        clue_key = (exposure.theme_id, exposure.ts_code)
        general_key = (exposure.theme_id, "")
        stock_event_score = clip(
            sum(clue_scores.get(clue_key, [])) / max(len(clue_scores.get(clue_key, [])), 1)
            if clue_scores.get(clue_key)
            else sum(clue_scores.get(general_key, [])) / max(len(clue_scores.get(general_key, [])), 1)
            if clue_scores.get(general_key)
            else 0.0,
            0.0,
            1.0,
        )
        exposure_score = clip(
            exposure.exposure_strength * 0.58
            + exposure.purity_score * 0.27
            + exposure.mapping_confidence * 0.15,
            0.0,
            1.0,
        )
        underpricing_score = clip(safe_float(price_row.get("underpricing_score"), thesis.score_card.underpricing_score), 0.0, 1.0)
        crowding_penalty = clip(safe_float(price_row.get("crowding_penalty"), thesis.score_card.crowding_penalty), 0.0, 1.0)
        final_score = clip(
            thesis.score_card.evidence_score * 0.24
            + thesis.score_card.causal_clarity_score * 0.12
            + thesis.score_card.persistence_score * 0.16
            + exposure_score * 0.21
            + underpricing_score * 0.17
            + stock_event_score * 0.10
            - crowding_penalty * 0.10,
            0.0,
            1.0,
        )
        allow_entry = (
            thesis.allow_entry
            and exposure.benefit_direction == "long"
            and exposure_score >= safe_float(stock_policy.get("min_exposure_score"), 0.45)
            and final_score >= safe_float(stock_policy.get("hold_score"), 0.50)
        )
        if not allow_entry:
            signal_state = "watch" if thesis.state in {"hold", "watch"} else thesis.state
        elif final_score >= safe_float(stock_policy.get("entry_score"), 0.61):
            signal_state = "entry"
        else:
            signal_state = "hold"
        signals.append(
            StockSignal(
                symbol=exposure.ts_code,
                ts_code=exposure.ts_code,
                code=exposure.code,
                name=exposure.name,
                trade_date=as_of_date,
                date=as_of_date,
                theme_id=exposure.theme_id,
                theme_name=exposure.theme_name,
                thesis_id=thesis.thesis_id,
                shock_type=thesis.shock_type,
                mechanism_primary=exposure.mechanism_primary,
                industry_primary=exposure.industry_primary,
                subchain_primary=exposure.subchain_primary,
                chain_position=exposure.chain_position,
                benefit_direction=exposure.benefit_direction,
                profit_path=exposure.profit_path,
                evidence_score=thesis.score_card.evidence_score,
                causal_clarity_score=thesis.score_card.causal_clarity_score,
                persistence_score=thesis.score_card.persistence_score,
                exposure_score=round(exposure_score, 4),
                underpricing_score=round(underpricing_score, 4),
                crowding_penalty=round(crowding_penalty, 4),
                final_score=round(final_score, 4),
                allow_entry=allow_entry,
                signal_state=signal_state,
                thesis_state=thesis.state,
                thesis_support_score=thesis.score_card.final_score,
                stock_specific_event_score=round(stock_event_score, 4),
                mapping_confidence=round(exposure.mapping_confidence, 4),
                latest_close=round(safe_float(price_row.get("latest_close"), 0.0), 4),
                price_date=safe_text(price_row.get("price_date")),
                price_source=safe_text(price_row.get("price_source")),
                event_clue_count=clue_counts.get(clue_key, clue_counts.get(general_key, 0)),
                non_event_evidence_count=thesis.evidence_bundle.non_event_count,
                evidence_types="|".join(thesis.evidence_bundle.evidence_types),
                reason_top=f"{thesis.theme_name}:{exposure.profit_path or exposure.chain_position}",
                reason_detail_json=safe_json_text(
                    {
                        "theme_id": thesis.theme_id,
                        "thesis_state": thesis.state,
                        "thesis_score": thesis.score_card.final_score,
                        "exposure_strength": exposure.exposure_strength,
                        "purity_score": exposure.purity_score,
                        "mapping_confidence": exposure.mapping_confidence,
                        "stock_event_score": stock_event_score,
                        "underpricing_score": underpricing_score,
                        "crowding_penalty": crowding_penalty,
                    }
                ),
            )
        )
    deduped = sorted(signals, key=lambda item: (-item.final_score, item.ts_code))
    seen: set[str] = set()
    out: List[StockSignal] = []
    for item in deduped:
        if item.ts_code in seen:
            continue
        seen.add(item.ts_code)
        out.append(item)
    return sorted(out, key=lambda item: item.final_score, reverse=True)
