# -*- coding: utf-8 -*-
"""事件抽取层：规则稳健排序 + 本地 Ollama + 反过拟合护栏。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config_utils import ensure_dir
from .local_ollama_worker import batch_parse_titles, rule_fallback_for_title
from .logging_utils import log_line

RULE_KEYWORDS = [
    ("业绩预告", "earnings_preannounce"),
    ("业绩快报", "earnings_flash"),
    ("年度报告", "financial_report"),
    ("半年度报告", "financial_report"),
    ("一季度报告", "financial_report"),
    ("三季度报告", "financial_report"),
    ("回购", "buyback_dividend"),
    ("分红", "buyback_dividend"),
    ("权益分派", "buyback_dividend"),
    ("增持", "management_trade"),
    ("减持", "management_trade"),
    ("问询", "regulatory_action"),
    ("处罚", "regulatory_action"),
    ("停牌", "trading_status_risk"),
    ("复牌", "trading_status_risk"),
    ("诉讼", "litigation_arbitration"),
    ("仲裁", "litigation_arbitration"),
    ("重大合同", "major_contract"),
    ("中标", "major_contract"),
    ("重组", "mna_restructure"),
    ("收购", "mna_restructure"),
    ("并购", "mna_restructure"),
    ("定向增发", "financing_event"),
    ("发行股份", "financing_event"),
    ("可转债", "financing_event"),
    ("风险提示", "risk_warning"),
    ("要约收购", "mna_restructure"),
    ("股份解除限售", "capital_flow_event"),
    ("授信额度", "governance_routine"),
    ("理财产品", "governance_routine"),
    ("股东会", "governance_routine"),
    ("股东大会", "governance_routine"),
    ("董事会", "governance_routine"),
    ("监事会", "governance_routine"),
    ("法律意见", "governance_routine"),
    ("声明与承诺", "governance_routine"),
]

POSITIVE_HINTS = [
    "回购", "增持", "中标", "重大合同", "预增", "扭亏", "分红", "摘帽", "收购", "重组",
]
NEGATIVE_HINTS = [
    "减持", "处罚", "问询", "诉讼", "仲裁", "停牌", "违约", "风险提示", "预亏", "减值", "终止",
]
SECTOR_MARKET_HINTS = [
    "行业", "板块", "政策", "国务院", "央行", "财政部", "证监会", "工信部", "交易所", "指导意见",
]
ROUTINE_TITLE_HINTS = [
    "提示性公告", "会议决议", "董事会", "监事会", "股东会", "股东大会", "声明与承诺", "法律意见",
    "章程", "注册资本", "住所变更", "理财产品", "授信额度", "补充公告", "更正公告", "回复函",
]

OLLAMA_EVENT_TYPE_MAP = {
    "财务业绩": "financial_report",
    "分红回购": "buyback_dividend",
    "增减持": "management_trade",
    "并购重组": "mna_restructure",
    "重大合同": "major_contract",
    "监管处罚": "regulatory_action",
    "停复牌": "trading_status_risk",
    "诉讼仲裁": "litigation_arbitration",
    "其他": "policy_industry_event",
}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _stable_event_id(item: Dict[str, Any]) -> str:
    stable_payload = {
        "source_type": _safe_text(item.get("source_type")),
        "source_name": _safe_text(item.get("source_name")),
        "publish_time": _safe_text(item.get("publish_time")),
        "title": _safe_text(item.get("title") or item.get("raw_title")),
        "url": _safe_text(item.get("url")),
        "security_code_hint": _safe_text(item.get("security_code_hint") or item.get("security_code")),
        "company_name_hint": _safe_text(item.get("company_name_hint") or item.get("company_name")),
    }
    return hashlib.md5(json.dumps(stable_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_dt(value: str) -> datetime | None:
    text = _safe_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _hours_since(value: str) -> float | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)


def _normalize_title(title: str) -> str:
    text = _safe_text(title)
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[（(].*?[)）]", "", text)
    text = re.sub(r"\d{4}年|\d{1,2}月|\d{1,2}日", "", text)
    text = re.sub(r"[A-Za-z0-9._\-:/]+", "", text)
    text = re.sub(r"[^\u4e00-\u9fff]+", "", text)
    return text[:48]


def _guess_event_type(title: str) -> str:
    for keyword, mapped in RULE_KEYWORDS:
        if keyword in title:
            return mapped
    return "policy_industry_event"


def _normalize_ollama_event_type(raw_type: str, title: str) -> str:
    mapped = OLLAMA_EVENT_TYPE_MAP.get(_safe_text(raw_type))
    if mapped:
        return mapped
    return _guess_event_type(title)


def _source_weight(item: Dict[str, Any]) -> float:
    source_type = _safe_text(item.get("source_type"))
    source_name = _safe_text(item.get("source_name")).lower()
    if source_type == "announcement":
        weight = 1.0
    elif "major_news" in source_name:
        weight = 0.82
    elif source_type == "news":
        weight = 0.64
    else:
        weight = 0.56
    if "cninfo" in source_name:
        weight += 0.05
    elif "sse" in source_name or "szse" in source_name:
        weight += 0.03
    elif "wallstreetcn" in source_name or "财联社" in source_name:
        weight += 0.02
    return _clamp(weight, 0.0, 1.05)


def _entity_strength(item: Dict[str, Any]) -> float:
    if _safe_text(item.get("security_code_hint") or item.get("security_code")):
        return 1.0
    if _safe_text(item.get("company_name_hint") or item.get("company_name")):
        return 0.65
    return 0.0


def _keyword_signal(item: Dict[str, Any], keywords: List[str]) -> Tuple[List[str], List[str], float]:
    title = _safe_text(item.get("title") or item.get("raw_title"))
    content = _safe_text(item.get("content") or item.get("raw_text"))[:1200]
    title_hits = [k for k in keywords if k and k in title]
    content_hits = [k for k in keywords[:12] if k and k not in title_hits and k in content]
    score = min(0.72, 0.28 * len(title_hits) + 0.08 * len(content_hits))
    return title_hits[:6], content_hits[:6], round(score, 4)


def _content_strength(item: Dict[str, Any]) -> float:
    content = _safe_text(item.get("content") or item.get("raw_text"))
    score = min(0.28, len(content[:1800]) / 3200.0)
    if _safe_text(item.get("pdf_local_path") or item.get("pdf_path")):
        score += 0.16
    if _safe_text(item.get("url")):
        score += 0.05
    return _clamp(score, 0.0, 0.45)


def _recency_score(item: Dict[str, Any]) -> Tuple[float, float]:
    hours = _hours_since(_safe_text(item.get("publish_time")) or _safe_text(item.get("crawl_time")))
    if hours is None:
        return 0.55, 999.0
    if hours <= 6:
        return 1.0, hours
    if hours <= 24:
        return 0.88, hours
    if hours <= 72:
        return 0.72, hours
    if hours <= 168:
        return 0.58, hours
    return 0.42, hours


def _anti_overfit_weight(item: Dict[str, Any], event_type: str, title_hits: List[str]) -> float:
    title = _safe_text(item.get("title") or item.get("raw_title"))
    content = _safe_text(item.get("content") or item.get("raw_text"))
    normalized_title = _normalize_title(title)
    penalty = 0.0
    if event_type == "governance_routine":
        penalty += 0.36
    if any(token in title for token in ROUTINE_TITLE_HINTS):
        penalty += 0.22
    if not _safe_text(item.get("security_code_hint") or item.get("security_code")) and not _safe_text(item.get("company_name_hint") or item.get("company_name")):
        penalty += 0.18
    if not title_hits and _safe_text(item.get("source_type")) == "news":
        penalty += 0.16
    if len(normalized_title) <= 10:
        penalty += 0.08
    if not content and not _safe_text(item.get("pdf_local_path") or item.get("pdf_path")):
        penalty += 0.12
    if any(token in title for token in ("摘要", "提示性", "声明", "承诺", "回复")):
        penalty += 0.08
    return _clamp(1.0 - penalty, 0.18, 1.0)


def _infer_event_direction(title: str, content: str, event_type: str) -> str:
    text = f"{_safe_text(title)} {_safe_text(content)[:300]}"
    pos_hits = sum(token in text for token in POSITIVE_HINTS)
    neg_hits = sum(token in text for token in NEGATIVE_HINTS)
    if event_type in {"regulatory_action", "litigation_arbitration", "risk_warning"}:
        neg_hits += 1
    if event_type in {"buyback_dividend", "major_contract"}:
        pos_hits += 1
    if pos_hits > neg_hits:
        return "positive"
    if neg_hits > pos_hits:
        return "negative"
    return "uncertain"


def _infer_impact_scope(item: Dict[str, Any], title: str, event_type: str) -> str:
    if _safe_text(item.get("security_code_hint") or item.get("security_code")):
        return "single_name"
    if _safe_text(item.get("company_name_hint") or item.get("company_name")):
        return "single_name"
    if event_type == "policy_industry_event" or any(token in title for token in SECTOR_MARKET_HINTS):
        return "sector"
    return "market"


def _infer_impact_horizon(event_type: str, source_type: str) -> str:
    if event_type in {"financial_report", "earnings_preannounce", "earnings_flash", "mna_restructure", "major_contract"}:
        return "1_3m"
    if event_type in {"buyback_dividend", "management_trade", "litigation_arbitration", "regulatory_action", "financing_event"}:
        return "1_4w"
    if event_type in {"trading_status_risk", "risk_warning"}:
        return "1_3d"
    if source_type == "announcement":
        return "1_4w"
    return "1_3d"


def _theme_key(item: Dict[str, Any], event_type: str) -> str:
    entity = _safe_text(item.get("security_code_hint")) or _safe_text(item.get("company_name_hint")) or "market"
    return f"{entity}|{event_type}"


def _cluster_key(item: Dict[str, Any], event_type: str) -> str:
    entity = _safe_text(item.get("security_code_hint")) or _safe_text(item.get("company_name_hint")) or _safe_text(item.get("source_name")) or "market"
    return f"{entity}|{event_type}|{_normalize_title(_safe_text(item.get('title') or item.get('raw_title')))}"


def _annotate_items(config: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keywords = list(config.get("event_ingest", {}).get("high_value_title_keywords", []) or [])
    enriched: List[Dict[str, Any]] = []
    theme_sources: Dict[str, set[str]] = defaultdict(set)
    cluster_counts: Counter[str] = Counter()
    theme_counts: Counter[str] = Counter()

    for item in raw_items:
        cp = dict(item)
        title = _safe_text(cp.get("title") or cp.get("raw_title"))
        event_type_hint = _guess_event_type(title)
        title_hits, content_hits, keyword_score = _keyword_signal(cp, keywords)
        recency_score, recency_hours = _recency_score(cp)
        cp["_event_type_hint"] = event_type_hint
        cp["_title_keyword_hits"] = title_hits
        cp["_content_keyword_hits"] = content_hits
        cp["_keyword_score"] = keyword_score
        cp["_source_weight"] = _source_weight(cp)
        cp["_entity_strength"] = _entity_strength(cp)
        cp["_content_strength"] = _content_strength(cp)
        cp["_recency_score"] = recency_score
        cp["_recency_hours"] = round(recency_hours, 3)
        cp["_anti_overfit_weight"] = _anti_overfit_weight(cp, event_type_hint, title_hits)
        cp["_theme_key"] = _theme_key(cp, event_type_hint)
        cp["_cluster_key"] = _cluster_key(cp, event_type_hint)
        enriched.append(cp)
        theme_sources[cp["_theme_key"]].add(_safe_text(cp.get("source_name")) or _safe_text(cp.get("source_type")) or "unknown")
        theme_counts[cp["_theme_key"]] += 1
        cluster_counts[cp["_cluster_key"]] += 1

    for cp in enriched:
        corroboration_count = max(cluster_counts[cp["_cluster_key"]], len(theme_sources[cp["_theme_key"]]))
        corroboration_bonus = min(0.42, 0.12 * max(0, corroboration_count - 1) + 0.05 * max(0, theme_counts[cp["_theme_key"]] - 1))
        anti_weight = float(cp["_anti_overfit_weight"])
        rule_score = (
            0.95 * float(cp["_source_weight"])
            + float(cp["_keyword_score"])
            + 0.35 * float(cp["_entity_strength"])
            + 0.60 * float(cp["_content_strength"])
            + 0.45 * float(cp["_recency_score"])
            + corroboration_bonus
            - 0.80 * (1.0 - anti_weight)
        )
        evidence_quality = (
            0.26 * (float(cp["_source_weight"]) / 1.05)
            + 0.18 * float(cp["_entity_strength"])
            + 0.16 * float(cp["_recency_score"])
            + 0.12 * min(1.0, float(cp["_content_strength"]) / 0.45 if 0.45 else 0.0)
            + 0.14 * min(1.0, corroboration_count / 3.0)
            + 0.14 * anti_weight
        )
        impact_scope = _infer_impact_scope(cp, _safe_text(cp.get("title") or cp.get("raw_title")), cp["_event_type_hint"])
        scope_bonus = 1.0 if impact_scope == "single_name" else 0.72 if impact_scope == "sector" else 0.56
        research_priority = (
            0.54 * _clamp(evidence_quality, 0.0, 1.0)
            + 0.26 * _clamp(rule_score / 2.8, 0.0, 1.0)
            + 0.12 * anti_weight
            + 0.08 * scope_bonus
        )
        cp["_impact_scope"] = impact_scope
        cp["_impact_horizon"] = _infer_impact_horizon(cp["_event_type_hint"], _safe_text(cp.get("source_type")))
        cp["_corroboration_count"] = int(corroboration_count)
        cp["_source_diversity"] = int(len(theme_sources[cp["_theme_key"]]))
        cp["_theme_density"] = int(theme_counts[cp["_theme_key"]])
        cp["_rule_score"] = round(rule_score, 4)
        cp["_evidence_quality_score"] = round(_clamp(evidence_quality, 0.0, 1.0), 4)
        cp["_research_priority_score"] = round(_clamp(research_priority, 0.0, 1.0), 4)
    return enriched


def _build_structured_facts(raw: Dict[str, Any], parsed: Dict[str, Any], final_event_type: str) -> Dict[str, Any]:
    extract_ok = bool(parsed.get("extract_ok", False))
    return {
        "announcement_category": raw.get("announcement_category"),
        "market_hint": raw.get("market_hint"),
        "url": raw.get("url"),
        "pdf_local_path": raw.get("pdf_local_path") or raw.get("pdf_path"),
        "rule_score": float(raw.get("_rule_score", 0.0) or 0.0),
        "evidence_quality_score": float(raw.get("_evidence_quality_score", 0.0) or 0.0),
        "research_priority_score": float(raw.get("_research_priority_score", 0.0) or 0.0),
        "source_weight": float(raw.get("_source_weight", 0.0) or 0.0),
        "source_diversity": int(raw.get("_source_diversity", 1) or 1),
        "corroboration_count": int(raw.get("_corroboration_count", 1) or 1),
        "theme_density": int(raw.get("_theme_density", 1) or 1),
        "recency_hours": float(raw.get("_recency_hours", 999.0) or 999.0),
        "anti_overfit_weight": float(raw.get("_anti_overfit_weight", 1.0) or 1.0),
        "impact_scope": _safe_text(raw.get("_impact_scope")),
        "impact_horizon": _safe_text(raw.get("_impact_horizon")),
        "keyword_hits": list(raw.get("_title_keyword_hits", []) or []),
        "content_keyword_hits": list(raw.get("_content_keyword_hits", []) or []),
        "theme_key": _safe_text(raw.get("_theme_key")),
        "cluster_key": _safe_text(raw.get("_cluster_key")),
        "event_type_hint": _safe_text(raw.get("_event_type_hint")),
        "normalized_event_type": final_event_type,
        "ollama_event_type": parsed.get("event_type"),
        "ollama_importance": parsed.get("importance"),
        "ollama_summary": parsed.get("summary"),
        "extract_ok": extract_ok,
        "extract_error": _safe_text(parsed.get("extract_error")),
        "title_only_signal": not bool(_safe_text(raw.get("content") or raw.get("raw_text"))),
    }


def _fallback_event(item: Dict[str, Any], score: float) -> Dict[str, Any]:
    title = _safe_text(item.get("title") or item.get("raw_title"))
    raw_text = _safe_text(item.get("content") or item.get("raw_text"))
    rule = rule_fallback_for_title(title)
    final_event_type = _normalize_ollama_event_type(_safe_text(rule.get("event_type")), title)
    evidence_quality = float(item.get("_evidence_quality_score", 0.0) or 0.0)
    anti_weight = float(item.get("_anti_overfit_weight", 1.0) or 1.0)
    corroboration_norm = min(1.0, float(item.get("_corroboration_count", 1) or 1) / 3.0)
    importance_score = _clamp(
        0.36 * evidence_quality
        + 0.28 * _clamp(float(rule.get("importance", 0) or 0) / 10.0, 0.0, 1.0)
        + 0.20 * anti_weight
        + 0.16 * corroboration_norm,
        0.18,
        0.96,
    )
    confidence = _clamp(
        0.22
        + 0.28 * evidence_quality
        + 0.18 * anti_weight
        + 0.12 * corroboration_norm,
        0.16,
        0.82,
    )
    return {
        "event_id": _stable_event_id(item),
        "publish_time": _safe_text(item.get("publish_time")),
        "crawl_time": _safe_text(item.get("crawl_time")),
        "source_type": _safe_text(item.get("source_type")),
        "source_name": _safe_text(item.get("source_name")),
        "security_code": _safe_text(item.get("security_code_hint")),
        "company_name": _safe_text(item.get("company_name_hint")),
        "industry_tags": [],
        "event_type": final_event_type,
        "event_direction": _infer_event_direction(title, raw_text, final_event_type),
        "impact_scope": _safe_text(item.get("_impact_scope")),
        "impact_horizon": _safe_text(item.get("_impact_horizon")),
        "confidence": round(confidence, 4),
        "novelty_score": round(_clamp(0.55 * anti_weight + 0.45 * max(0.0, 1.0 - corroboration_norm), 0.0, 1.0), 4),
        "importance_score": round(importance_score, 4),
        "raw_title": title,
        "raw_text": raw_text,
        "structured_facts": _build_structured_facts(item, rule, final_event_type),
        "extract_model": _safe_text(rule.get("extract_backend") or "rule_fallback"),
        "review_status": "review_required" if (evidence_quality < 0.46 or anti_weight < 0.48) else "auto_fallback",
    }


def _normalize_ollama_item(raw: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    title = _safe_text(raw.get("title") or raw.get("raw_title"))
    raw_text = _safe_text(raw.get("content") or raw.get("raw_text"))
    final_event_type = _normalize_ollama_event_type(_safe_text(item.get("event_type")), title)
    evidence_quality = float(raw.get("_evidence_quality_score", 0.0) or 0.0)
    anti_weight = float(raw.get("_anti_overfit_weight", 1.0) or 1.0)
    corroboration_norm = min(1.0, float(raw.get("_corroboration_count", 1) or 1) / 3.0)
    importance = _clamp(float(item.get("importance", 0) or 0) / 10.0, 0.0, 1.0)
    importance_score = _clamp(
        0.38 * evidence_quality
        + 0.34 * max(0.3, importance)
        + 0.18 * anti_weight
        + 0.10 * corroboration_norm,
        0.22,
        0.99,
    )
    confidence = _clamp(
        (0.44 if item.get("extract_ok") else 0.24)
        + 0.24 * evidence_quality
        + 0.12 * anti_weight
        + 0.10 * corroboration_norm,
        0.18,
        0.97,
    )
    return {
        "event_id": _stable_event_id(raw),
        "publish_time": _safe_text(raw.get("publish_time")),
        "crawl_time": _safe_text(raw.get("crawl_time")),
        "source_type": _safe_text(raw.get("source_type")),
        "source_name": _safe_text(raw.get("source_name")),
        "security_code": _safe_text(raw.get("security_code_hint")),
        "company_name": _safe_text(item.get("entity")) or _safe_text(raw.get("company_name_hint")),
        "industry_tags": [],
        "event_type": final_event_type,
        "event_direction": _infer_event_direction(title, raw_text, final_event_type),
        "impact_scope": _safe_text(raw.get("_impact_scope")),
        "impact_horizon": _safe_text(raw.get("_impact_horizon")),
        "confidence": round(confidence, 4),
        "novelty_score": round(_clamp(0.62 * anti_weight + 0.38 * max(0.0, 1.0 - corroboration_norm), 0.0, 1.0), 4),
        "importance_score": round(importance_score, 4),
        "raw_title": title,
        "raw_text": raw_text,
        "structured_facts": _build_structured_facts(raw, item, final_event_type),
        "extract_model": _safe_text(item.get("extract_backend") or "ollama"),
        "review_status": "auto_pass" if (item.get("extract_ok") and evidence_quality >= 0.42 and anti_weight >= 0.45) else "review_required",
    }


def _select_items(config: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    extract_cfg = dict(config.get("event_extract", {}) or {})
    max_events = int(extract_cfg.get("max_events_per_run", 20) or 20)
    llm_min_score = float(extract_cfg.get("llm_min_score", 1.35) or 1.35)
    annotated = _annotate_items(config=config, raw_items=raw_items)
    annotated.sort(
        key=lambda x: (
            float(x.get("_research_priority_score", 0.0) or 0.0),
            float(x.get("_evidence_quality_score", 0.0) or 0.0),
            str(x.get("publish_time", "")),
        ),
        reverse=True,
    )

    selected: List[Dict[str, Any]] = []
    seen_clusters: set[str] = set()
    theme_counts: Counter[str] = Counter()
    for item in annotated:
        cluster_key = _safe_text(item.get("_cluster_key"))
        theme_key = _safe_text(item.get("_theme_key"))
        quality = float(item.get("_evidence_quality_score", 0.0) or 0.0)
        anti_weight = float(item.get("_anti_overfit_weight", 1.0) or 1.0)
        if cluster_key in seen_clusters:
            continue
        if theme_counts[theme_key] >= 2 and float(item.get("_research_priority_score", 0.0) or 0.0) < 0.84:
            continue
        if quality < 0.33 and anti_weight < 0.40:
            continue
        selected.append(item)
        seen_clusters.add(cluster_key)
        theme_counts[theme_key] += 1
        if len(selected) >= max_events:
            break

    if not selected:
        selected = annotated[:max(1, min(4, len(annotated)))]

    llm_items: List[Dict[str, Any]] = []
    fallback_only: List[Dict[str, Any]] = []
    for item in selected:
        rule_score = float(item.get("_rule_score", 0.0) or 0.0)
        evidence_quality = float(item.get("_evidence_quality_score", 0.0) or 0.0)
        anti_weight = float(item.get("_anti_overfit_weight", 1.0) or 1.0)
        if rule_score >= llm_min_score or (evidence_quality >= 0.70 and anti_weight >= 0.55):
            llm_items.append(item)
        else:
            fallback_only.append(item)
    return llm_items, fallback_only


def extract_events_with_worker(config: Dict[str, Any], raw_items: List[Dict[str, Any]], prompt_root: Path) -> List[Dict[str, Any]]:
    """兼容 orchestrator 的事件抽取入口。"""
    extract_cfg = dict(config.get("event_extract", {}) or {})
    llm_items, fallback_only = _select_items(config=config, raw_items=raw_items)

    results: List[Dict[str, Any]] = []
    for item in fallback_only:
        results.append(_fallback_event(item, float(item.get("_rule_score", 0.0) or 0.0)))

    if llm_items:
        model = str(
            config.get("local_ollama", {}).get("event_extract_model")
            or config.get("local_ollama", {}).get("model")
            or "qwen2.5:7b"
        )
        log_line(config, f"事件抽取进入本地模型：本地事件数={len(llm_items)} model={model}")
        parsed_items = batch_parse_titles(items=llm_items, config=config)
        for idx, (raw, parsed) in enumerate(zip(llm_items, parsed_items), start=1):
            log_line(config, f"Ollama 本地抽取完成：item={idx}/{len(llm_items)} 成功")
            results.append(_normalize_ollama_item(raw, parsed))

    results.sort(
        key=lambda x: (
            float(x.get("structured_facts", {}).get("research_priority_score", 0.0) or 0.0),
            float(x.get("importance_score", 0.0) or 0.0),
        ),
        reverse=True,
    )

    if bool(extract_cfg.get("save_extract_summary", True)):
        root = ensure_dir(Path(str(config["paths"]["research_root"])) / "extract_summary")
        summary = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "raw_events": len(raw_items),
            "selected_for_run": len(llm_items) + len(fallback_only),
            "llm_events": len(llm_items),
            "fallback_events": len(fallback_only),
            "backend": "local_ollama",
            "model": str(
                config.get("local_ollama", {}).get("event_extract_model")
                or config.get("local_ollama", {}).get("model")
                or "qwen2.5:7b"
            ),
            "avg_evidence_quality": round(
                sum(float(x.get("structured_facts", {}).get("evidence_quality_score", 0.0) or 0.0) for x in results) / max(len(results), 1),
                4,
            ),
            "avg_anti_overfit_weight": round(
                sum(float(x.get("structured_facts", {}).get("anti_overfit_weight", 0.0) or 0.0) for x in results) / max(len(results), 1),
                4,
            ),
        }
        (root / "event_extract_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def save_event_store(config: Dict[str, Any], events: List[Dict[str, Any]]) -> Path:
    root = Path(str(config["paths"]["event_store_root"]))
    ensure_dir(root)
    out_path = root / "event_store.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        for item in events:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return out_path
