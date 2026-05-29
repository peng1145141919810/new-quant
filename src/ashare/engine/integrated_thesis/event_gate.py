from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .contracts import PRIMARY_EVENT_TYPES


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _coarse_event_type(raw: Dict[str, Any]) -> str:
    blob = " ".join(
        [
            _text(raw.get("event_type")),
            _text(raw.get("raw_title")),
            _text(raw.get("summary")),
            _text(dict(raw.get("structured_facts", {}) or {}).get("normalized_event_type")),
        ]
    ).lower()
    if any(token in blob for token in ["政策", "招标", "中标", "补贴", "出口", "关税", "采购", "tender", "policy"]):
        return "policy_supply_chain"
    if any(token in blob for token in ["预增", "预减", "业绩", "快报", "forecast", "express", "guidance"]):
        return "earnings_guidance"
    if any(token in blob for token in ["扩产", "产能", "capex", "项目", "投产", "开工"]):
        return "capacity_capex"
    if any(token in blob for token in ["价格", "库存", "价差", "price", "inventory", "warehouse"]):
        return "price_inventory"
    if any(token in blob for token in ["风险", "减持", "诉讼", "问询", "处罚", "停牌", "风险提示"]):
        return "risk_event"
    return "policy_supply_chain"


def _symbols(raw: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    candidates: Iterable[Any] = (
        list(raw.get("related_symbols", []) or [])
        + [_text(raw.get("security_code"))]
        + [_text(raw.get("ts_code"))]
    )
    for item in candidates:
        text = _text(item).upper()
        if text and text not in out:
            out.append(text)
    return out


def build_event_cards(structured_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for idx, raw in enumerate(structured_events or []):
        facts = dict(raw.get("structured_facts", {}) or {})
        event_type = _coarse_event_type(raw)
        if event_type not in PRIMARY_EVENT_TYPES:
            continue
        importance = _float(raw.get("importance_score", facts.get("importance_score", 0.0)))
        evidence = _float(raw.get("evidence_quality_score", facts.get("evidence_quality_score", 0.0)))
        anti_noise = _float(raw.get("anti_overfit_weight", facts.get("anti_overfit_weight", 1.0)), 1.0)
        source_bonus = 0.10 if _text(raw.get("source_type")).lower() == "announcement" else 0.0
        event_quality = _clip(importance * 0.45 + evidence * 0.45 + anti_noise * 0.10 + source_bonus)
        if event_quality < 0.20:
            continue
        cards.append(
            {
                "event_id": _text(raw.get("event_id")) or f"event_{idx+1:04d}",
                "event_date": _text(raw.get("publish_time") or raw.get("event_date") or raw.get("raw_time")),
                "event_type": event_type,
                "direction": _text(raw.get("event_direction") or facts.get("direction") or "positive").lower() or "positive",
                "strength": round(event_quality, 4),
                "scope_type": _text(raw.get("impact_scope") or "single_name").lower() or "single_name",
                "scope_target": _text(raw.get("company_name") or raw.get("security_name") or facts.get("company_name")),
                "source_quality": round(evidence, 4),
                "horizon_type": _text(raw.get("impact_horizon") or "swing").lower() or "swing",
                "trigger_company": _text(raw.get("company_name") or raw.get("security_name")),
                "related_symbols": _symbols(raw),
                "manual_attention_flag": bool(_float(raw.get("manual_review", 0.0)) > 0),
                "is_research_proxy": bool(raw.get("is_research_proxy", False)),
                "event_quality": round(event_quality, 4),
                "summary": _text(raw.get("summary") or facts.get("ollama_summary") or raw.get("raw_title"))[:220],
            }
        )
    return cards

