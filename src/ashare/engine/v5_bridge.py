# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir


def _ensure_list(value: Any) -> List[Any]:
    """把输入规整成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_action_list(value: Any) -> List[Dict[str, Any]]:
    """把 brief 里的 action 字段规整成对象列表。"""
    items = _ensure_list(value)
    normalized: List[Dict[str, Any]] = []

    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append({"action": item, "raw_text": item})
        else:
            normalized.append({"action": str(item), "raw_text": str(item)})

    return normalized


def _append_texts(texts: List[str], value: Any) -> None:
    """递归收集所有可映射文本。"""
    if isinstance(value, str):
        text = value.strip()
        if text:
            texts.append(text)
        return
    if isinstance(value, dict):
        for v in value.values():
            _append_texts(texts, v)
        return
    if isinstance(value, list):
        for item in value:
            _append_texts(texts, item)


def _collect_texts(brief: Dict[str, Any]) -> List[str]:
    """收集可用于映射的文本片段。"""
    texts: List[str] = []

    for key in [
        "research_thesis",
        "core_theses",
        "why_now",
        "research_mapping",
        "risk_note",
        "note",
        "summary",
        "today_change_research_direction",
        "today_fill_data",
        "priority_order_for_execution",
        "new_feature_candidates",
        "paused_branches",
        "evidence_assessment",
    ]:
        _append_texts(texts, brief.get(key))

    for field in [
        "core_theses",
        "feature_actions",
        "label_actions",
        "model_actions",
        "data_actions",
        "risk_actions",
        "portfolio_actions",
        "candidate_experiments",
        "priority_events",
    ]:
        _append_texts(texts, brief.get(field, []))

    return texts


def _extract_label_horizons(brief: Dict[str, Any]) -> List[int]:
    """从 brief 中抽取标签周期。"""
    horizons: List[int] = []

    for thesis in _ensure_list(brief.get("core_theses", [])):
        if isinstance(thesis, dict):
            for x in _ensure_list(thesis.get("target_labels", [])):
                if isinstance(x, str):
                    for g in re.findall(r"(\d+)\s*d", x.lower()):
                        horizons.append(int(g))

    # 1) 从 candidate_experiments 显式字段提取
    for exp in _ensure_list(brief.get("candidate_experiments", [])):
        if isinstance(exp, dict):
            labels = exp.get("labels") or exp.get("label_horizons") or []
            for x in _ensure_list(labels):
                try:
                    horizons.append(int(x))
                except Exception:
                    pass

            target_labels = exp.get("target_labels") or []
            for name in _ensure_list(target_labels):
                if isinstance(name, str):
                    m = re.findall(r"(\d+)d", name)
                    for g in m:
                        horizons.append(int(g))

    # 2) 从 label_actions 提取
    for action in _normalize_action_list(brief.get("label_actions", [])):
        raw_text = str(action.get("raw_text", "") or action.get("action", ""))
        if any(token in raw_text for token in ["暂停", "禁用", "不做", "删除", "移除"]):
            continue
        horizon = action.get("label_horizon")
        if horizon is not None:
            try:
                horizons.append(int(horizon))
            except Exception:
                pass

        for g in re.findall(r"(\d+)\s*d", raw_text.lower()):
            horizons.append(int(g))

    # 3) 从 thesis 摘要与核心摘要做有限兜底，避免把 event_panel_60d 之类数据名误识别成标签周期
    fallback_texts: List[str] = []
    for field in ["research_thesis", "why_now"]:
        value = brief.get(field)
        if isinstance(value, str) and value.strip():
            fallback_texts.append(value.strip())
    for text in fallback_texts:
        for g in re.findall(r"(\d+)\s*d", text.lower()):
            horizons.append(int(g))

    horizons = [x for x in horizons if x > 0]
    return sorted(list(dict.fromkeys(horizons)))[:6]


def _extract_feature_profiles(brief: Dict[str, Any]) -> List[str]:
    """从 brief 中抽取 feature profile。"""
    texts = " ".join(_collect_texts(brief)).lower()

    profiles: List[str] = []

    event_keywords = [
        "事件", "增持", "减持", "回购", "业绩", "仲裁", "诉讼", "中标", "风险提示",
        "earnings", "buyback", "event", "litigation", "surprise",
    ]
    vol_liq_keywords = [
        "流动性", "波动", "质量", "换手", "成交额", "liquidity", "volatility", "quality",
    ]
    momentum_keywords = [
        "动量", "横截面", "价量", "momentum", "cross section", "cross_section",
    ]

    if any(k in texts for k in event_keywords):
        profiles.append("generated_feature_pack")
    if any(k in texts for k in vol_liq_keywords):
        profiles.append("vol_liq_quality")
    if any(k in texts for k in momentum_keywords):
        profiles.append("momentum_cross_section")

    if not profiles:
        profiles.append("generated_feature_pack")

    return list(dict.fromkeys(profiles))


def _extract_model_families(brief: Dict[str, Any]) -> List[str]:
    """从 brief 中抽取模型族。"""
    families: List[str] = []

    for exp in _ensure_list(brief.get("candidate_experiments", [])):
        if isinstance(exp, dict):
            for model in _ensure_list(exp.get("models", [])):
                if isinstance(model, str) and model.strip():
                    families.append(model.strip())

    for action in _normalize_action_list(brief.get("model_actions", [])):
        fam = action.get("model_family")
        if fam:
            families.append(str(fam))

    if not families:
        families = ["xgboost_gpu", "lightgbm_auto"]

    # 保留常见族
    allowed = {
        "xgboost_gpu",
        "ridge_ranker",
        "lightgbm_auto",
        "lightgbm_gpu",
    }
    filtered = [x for x in families if x in allowed]
    if not filtered:
        filtered = ["xgboost_gpu", "lightgbm_auto"]

    return list(dict.fromkeys(filtered))


def _build_route_override(brief: Dict[str, Any]) -> Dict[str, int]:
    """动态生成 route override。"""
    texts = " ".join(_collect_texts(brief)).lower()

    route = {
        "feature": 2,
        "data": 1,
        "risk": 1,
        "portfolio": 1,
        "training": 1,
        "model": 1,
        "hybrid": 1,
    }

    if any(k in texts for k in ["事件", "增持", "减持", "回购", "业绩", "surprise", "event"]):
        route["feature"] += 1
        route["hybrid"] += 1

    if any(k in texts for k in ["风险", "诉讼", "仲裁", "drawdown", "risk"]):
        route["risk"] += 1
        route["portfolio"] += 1

    if any(k in texts for k in ["补数据", "刷新", "重算", "data", "refresh"]):
        route["data"] += 1

    if len(_ensure_list(brief.get("candidate_experiments", []))) >= 6:
        route["feature"] += 1
        route["portfolio"] += 1

    return route


def build_research_actions(brief: Dict[str, Any]) -> Dict[str, Any]:
    """把 research brief 翻译为 V5.1 可消费的桥接动作。"""
    route_override = _build_route_override(brief)
    label_horizons = _extract_label_horizons(brief)
    feature_profiles = _extract_feature_profiles(brief)
    model_families = _extract_model_families(brief)

    top_ks = [10, 20]
    if any(h <= 5 for h in label_horizons):
        top_ks = [10, 20]
    elif any(h >= 20 for h in label_horizons):
        top_ks = [20]

    candidate_override = {
        "feature_profiles": feature_profiles,
        "label_horizons": label_horizons or [5, 10],
        "top_ks": top_ks,
        "preferred_model_families": model_families,
        "ban_model_families": [],
    }

    actions = {
        "route_override": route_override,
        "candidate_override": candidate_override,
        "context_snapshot": {
            "research_thesis": brief.get("research_thesis", ""),
            "why_now": brief.get("why_now", ""),
            "priority_events": _ensure_list(brief.get("priority_events", [])),
            "candidate_experiments": _ensure_list(brief.get("candidate_experiments", [])),
            "feature_actions": _ensure_list(brief.get("feature_actions", [])),
            "risk_actions": _ensure_list(brief.get("risk_actions", [])),
        },
    }
    return actions


def save_bridge_outputs(config: Dict[str, Any], actions: Dict[str, Any]) -> None:
    """保存 bridge 文件。"""
    root = Path(str(config["paths"]["bridge_root"]))
    ensure_dir(root)

    (root / "llm_route_override.json").write_text(
        json.dumps(actions.get("route_override", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "candidate_override.json").write_text(
        json.dumps(actions.get("candidate_override", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "enriched_context.json").write_text(
        json.dumps(actions.get("context_snapshot", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
