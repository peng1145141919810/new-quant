# -*- coding: utf-8 -*-
"""V6 研究证据包生成器。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _float_metric(item: Dict[str, Any], key: str, default: float = 0.0) -> float:
    facts = dict(item.get("structured_facts", {}) or {})
    try:
        return float(item.get(key, facts.get(key, default)) or default)
    except Exception:
        return float(default)


def _int_metric(item: Dict[str, Any], key: str, default: int = 0) -> int:
    facts = dict(item.get("structured_facts", {}) or {})
    try:
        return int(item.get(key, facts.get(key, default)) or default)
    except Exception:
        return int(default)


def _event_sort_key(item: Dict[str, Any]) -> tuple[float, float, float]:
    return (
        _float_metric(item, "research_priority_score"),
        _float_metric(item, "importance_score"),
        _float_metric(item, "evidence_quality_score"),
    )


def _compact_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """压缩事件为可给研究脑直接消费的证据卡。"""
    facts = dict(item.get("structured_facts", {}) or {})
    summary = _safe_text(item.get("summary") or facts.get("ollama_summary") or item.get("raw_title"))
    return {
        "title": _safe_text(item.get("raw_title") or item.get("title"))[:120],
        "event_type": _safe_text(item.get("event_type") or facts.get("normalized_event_type") or facts.get("ollama_event_type") or "其他")[:48],
        "importance": int(round(_float_metric(item, "ollama_importance", _float_metric(item, "importance_score") * 10.0))),
        "summary": summary[:120],
        "source_type": _safe_text(item.get("source_type"))[:24],
        "source_name": _safe_text(item.get("source_name"))[:60],
        "security_code": _safe_text(item.get("security_code"))[:16],
        "company_name": _safe_text(item.get("company_name"))[:40],
        "event_direction": _safe_text(item.get("event_direction"))[:20],
        "impact_scope": _safe_text(item.get("impact_scope"))[:20],
        "impact_horizon": _safe_text(item.get("impact_horizon"))[:20],
        "evidence_quality": round(_float_metric(item, "evidence_quality_score"), 4),
        "research_priority": round(_float_metric(item, "research_priority_score"), 4),
        "source_diversity": _int_metric(item, "source_diversity", 1),
        "corroboration_count": _int_metric(item, "corroboration_count", 1),
        "anti_overfit_weight": round(_float_metric(item, "anti_overfit_weight", 1.0), 4),
    }


def _compact_evidence_card(item: Dict[str, Any]) -> Dict[str, Any]:
    """压缩公告证据卡，避免把长文本直接塞给研究脑。"""
    return {
        "title": _safe_text(item.get("title"))[:120],
        "publish_time": _safe_text(item.get("publish_time"))[:32],
        "source_name": _safe_text(item.get("source_name"))[:60],
        "security_code_hint": _safe_text(item.get("security_code_hint"))[:16],
        "company_name_hint": _safe_text(item.get("company_name_hint"))[:40],
        "signal_type": _safe_text(item.get("signal_type"))[:32],
        "signal_strength": _safe_text(item.get("signal_strength"))[:16],
        "impact_scope": _safe_text(item.get("impact_scope"))[:20],
        "impact_horizon": _safe_text(item.get("impact_horizon"))[:20],
        "key_points": [_safe_text(x)[:120] for x in list(item.get("key_points", []) or [])[:4] if _safe_text(x)],
        "research_angles": [_safe_text(x)[:120] for x in list(item.get("research_angles", []) or [])[:4] if _safe_text(x)],
        "risk_flags": [_safe_text(x)[:120] for x in list(item.get("risk_flags", []) or [])[:4] if _safe_text(x)],
        "why_relevant": _safe_text(item.get("why_relevant"))[:220],
    }


def build_research_context_pack(
    config: Dict[str, Any],
    structured_events: List[Dict[str, Any]],
    data_gap_report: Dict[str, Any],
    evidence_cards: List[Dict[str, Any]] | None = None,
    industry_router_payload: Dict[str, Any] | None = None,
    market_state_payload: Dict[str, Any] | None = None,
    integrated_thesis_payload: Dict[str, Any] | None = None,
    research_meta_feedback: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """构建研究证据包。"""
    max_priority_events = int(config.get("research_context_pack", {}).get("max_priority_events", 30) or 30)
    priority_events = sorted(structured_events, key=_event_sort_key, reverse=True)[:max_priority_events]
    compact_priority_events = [_compact_event(x) for x in priority_events]
    compact_evidence_cards = [_compact_evidence_card(x) for x in list(evidence_cards or [])[:4] if isinstance(x, dict)]
    industry_router_payload = dict(industry_router_payload or {})
    market_state_payload = dict(market_state_payload or {})
    integrated_thesis_payload = dict(integrated_thesis_payload or {})
    research_meta_feedback = dict(research_meta_feedback or {})

    source_type_counter = Counter(_safe_text(x.get("source_type")) or "unknown" for x in structured_events)
    event_type_counter = Counter(_safe_text(x.get("event_type")) or "其他" for x in structured_events)
    high_importance_events = sum(_float_metric(x, "importance_score") >= 0.72 for x in structured_events)
    high_quality_events = sum(_float_metric(x, "evidence_quality_score") >= 0.60 for x in structured_events)
    confirmed_events = sum(
        _int_metric(x, "source_diversity", 1) >= 2
        or _int_metric(x, "corroboration_count", 1) >= 2
        or _safe_text(x.get("source_type")) == "announcement"
        for x in structured_events
    )
    weak_signal_events = sum(
        _float_metric(x, "evidence_quality_score") < 0.45
        or _float_metric(x, "anti_overfit_weight", 1.0) < 0.50
        for x in structured_events
    )
    single_name_events = sum(_safe_text(x.get("impact_scope")) == "single_name" for x in structured_events)
    market_wide_events = sum(_safe_text(x.get("impact_scope")) == "market" for x in structured_events)
    avg_evidence_quality = round(
        sum(_float_metric(x, "evidence_quality_score") for x in structured_events) / max(len(structured_events), 1),
        4,
    )
    avg_anti_overfit = round(
        sum(_float_metric(x, "anti_overfit_weight", 1.0) for x in structured_events) / max(len(structured_events), 1),
        4,
    )
    confirmed_ratio = round(confirmed_events / max(len(structured_events), 1), 4)
    weak_signal_ratio = round(weak_signal_events / max(len(structured_events), 1), 4)
    announcement_ratio = round(source_type_counter.get("announcement", 0) / max(len(structured_events), 1), 4)
    message_profile = "mixed"
    if confirmed_ratio >= 0.55 and avg_anti_overfit >= 0.62:
        message_profile = "confirmed_single_name"
    elif weak_signal_ratio >= 0.55 and confirmed_ratio < 0.35:
        message_profile = "headline_noise"

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_summary": {
            "total_events": len(structured_events),
            "high_importance_events": high_importance_events,
            "high_quality_events": high_quality_events,
            "confirmed_events": confirmed_events,
            "weak_signal_events": weak_signal_events,
            "single_name_events": single_name_events,
            "market_wide_events": market_wide_events,
            "top_event_types": [name for name, _ in event_type_counter.most_common(5)],
            "source_type_breakdown": dict(source_type_counter),
            "avg_evidence_quality": avg_evidence_quality,
            "avg_anti_overfit_weight": avg_anti_overfit,
        },
        "message_evidence_profile": {
            "profile": message_profile,
            "confirmed_ratio": confirmed_ratio,
            "weak_signal_ratio": weak_signal_ratio,
            "announcement_ratio": announcement_ratio,
            "news_ratio": round(source_type_counter.get("news", 0) / max(len(structured_events), 1), 4),
        },
        "priority_events": priority_events,
        "compact_priority_events": compact_priority_events,
        "evidence_cards": compact_evidence_cards,
        "data_gap_report": data_gap_report,
        "market_state": {
            "market_regime": _safe_text(market_state_payload.get("market_regime") or "neutral"),
            "style_bias": _safe_text(market_state_payload.get("style_bias") or "balanced"),
            "mechanism_bias": _safe_text(market_state_payload.get("mechanism_bias") or "balanced"),
            "risk_budget_multiplier": round(float(market_state_payload.get("risk_budget_multiplier", 1.0) or 1.0), 4),
            "turnover_multiplier": round(float(market_state_payload.get("turnover_multiplier", 1.0) or 1.0), 4),
            "entry_strictness": round(float(market_state_payload.get("entry_strictness", 0.5) or 0.5), 4),
            "new_position_policy": _safe_text(market_state_payload.get("new_position_policy") or "allow"),
            "de_risk_hint": _safe_text(market_state_payload.get("de_risk_hint") or ""),
            "trend_score": round(float(market_state_payload.get("trend_score", 0.0) or 0.0), 4),
            "breadth_score": round(float(market_state_payload.get("breadth_score", 0.0) or 0.0), 4),
            "liquidity_score": round(float(market_state_payload.get("liquidity_score", 0.0) or 0.0), 4),
            "style_score": round(float(market_state_payload.get("style_score", 0.0) or 0.0), 4),
            "market_regime_score": round(float(market_state_payload.get("market_regime_score", 0.0) or 0.0), 4),
        },
        "recent_experiments": [],
        "family_state": {},
        "research_space": {
            "feature_profiles": ["baseline_plus", "generated_feature_pack"],
            "model_families": ["xgboost_gpu", "ridge_ranker"],
            "label_horizons": [5, 10, 20],
        },
        "industry_router": {
            "latest_date": _safe_text(industry_router_payload.get("latest_date")),
            "active_theses": list(industry_router_payload.get("active_theses", []) or [])[:8],
            "theme_overview": list(industry_router_payload.get("theme_overview", []) or [])[:10],
            "top_stock_signals": list(industry_router_payload.get("top_stock_signals", []) or [])[:8],
            "evidence_overview": list(industry_router_payload.get("evidence_overview", []) or [])[:12],
            "mechanism_overview": list(industry_router_payload.get("mechanism_overview", []) or [])[:6],
        },
        "integrated_thesis": {
            "formal_strategy_framework": _safe_text(integrated_thesis_payload.get("formal_strategy_framework") or "integrated_event_industry_earnings_alpha"),
            "primary_strategy_key": _safe_text(integrated_thesis_payload.get("primary_strategy_key") or "integrated_stock_alpha"),
            "portfolio_construction": dict(integrated_thesis_payload.get("portfolio_construction", {}) or {}),
            "summary": dict(integrated_thesis_payload.get("summary", {}) or {}),
            "top_candidates": list(integrated_thesis_payload.get("top_candidates", []) or [])[:10],
        },
        "execution_meta_feedback": {
            "generated_at": _safe_text(research_meta_feedback.get("generated_at")),
            "mechanism_execution_realization": list(research_meta_feedback.get("mechanism_execution_realization", []) or [])[:6],
            "repeated_non_executable_symbols": list(research_meta_feedback.get("repeated_non_executable_symbols", []) or [])[:12],
        },
    }


def save_research_context_pack(config: Dict[str, Any], pack: Dict[str, Any]) -> Path:
    """保存研究证据包。"""
    root = Path(str(config["paths"]["research_root"])) / "context_pack"
    ensure_dir(root)
    out_path = root / "research_context_pack.json"
    out_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    compact_path = root / "compact_context_pack.json"
    compact_path.write_text(json.dumps({
        "generated_at": pack.get("generated_at"),
        "event_summary": pack.get("event_summary", {}),
        "message_evidence_profile": pack.get("message_evidence_profile", {}),
        "priority_events": pack.get("compact_priority_events", []),
        "evidence_cards": pack.get("evidence_cards", []),
        "data_gap_report": pack.get("data_gap_report", {}),
        "market_state": pack.get("market_state", {}),
        "industry_router": pack.get("industry_router", {}),
        "integrated_thesis": pack.get("integrated_thesis", {}),
        "execution_meta_feedback": pack.get("execution_meta_feedback", {}),
        "research_space": pack.get("research_space", {}),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
