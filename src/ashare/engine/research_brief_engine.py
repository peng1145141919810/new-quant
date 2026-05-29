# -*- coding: utf-8 -*-
"""V6 研究计划生成器。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir
from .llm_router import LLMRouter


def load_prompt(prompt_path: Path) -> str:
    """读取提示词。"""
    return prompt_path.read_text(encoding="utf-8")


def _ensure_list(value: Any) -> List[Any]:
    """把输入规整为列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_text(value: Any) -> str:
    """把输入规整为去空白字符串。"""
    return str(value or "").strip()


def _string_list(value: Any, max_items: int = 8) -> List[str]:
    """提取字符串列表。"""
    items: List[str] = []
    for item in _ensure_list(value):
        text = _as_text(item)
        if text:
            items.append(text[:120])
    return list(dict.fromkeys(items))[:max_items]


def _int_list(value: Any, max_items: int = 6) -> List[int]:
    """提取整数列表。"""
    items: List[int] = []
    for item in _ensure_list(value):
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            items.append(int(item))
            continue
        text = _as_text(item)
        if not text:
            continue
        m = re.findall(r"(\d+)", text)
        for g in m:
            items.append(int(g))
    items = [x for x in items if 0 < x <= 60]
    return list(dict.fromkeys(items))[:max_items]


def _float_value(value: Any, default: float = 0.0) -> float:
    """安全提取浮点数。"""
    try:
        return float(value if value is not None else default)
    except Exception:
        return float(default)


def _priority_event_metric(item: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """从优先事件中读取数值指标。"""
    facts = dict(item.get("structured_facts", {}) or {})
    return _float_value(item.get(key, facts.get(key, default)), default)


def _priority_event_int(item: Dict[str, Any], key: str, default: int = 0) -> int:
    """从优先事件中读取整数指标。"""
    try:
        return int(round(_priority_event_metric(item, key, float(default))))
    except Exception:
        return int(default)


def _event_is_confirmed(item: Dict[str, Any]) -> bool:
    """判断事件是否具备基本交叉印证。"""
    if _priority_event_int(item, "corroboration_count", 1) >= 2:
        return True
    if _priority_event_int(item, "source_diversity", 1) >= 2:
        return True
    return _as_text(item.get("source_type")) == "announcement"


def _qualified_priority_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只保留足够稳健、不会明显带来标题级过拟合风险的事件。"""
    qualified: List[Dict[str, Any]] = []
    for item in _ensure_list(events):
        if not isinstance(item, dict):
            continue
        evidence_quality = _priority_event_metric(item, "evidence_quality", _priority_event_metric(item, "evidence_quality_score"))
        anti_overfit = _priority_event_metric(item, "anti_overfit_weight", 1.0)
        impact_scope = _as_text(item.get("impact_scope"))
        if evidence_quality < 0.52:
            continue
        if anti_overfit < 0.52:
            continue
        if _event_is_confirmed(item) or impact_scope == "single_name":
            qualified.append(item)
    return qualified


def _event_blob(events: List[Dict[str, Any]]) -> str:
    """压平事件文本，避免到处重复拼接。"""
    return " ".join(
        (
            _as_text(item.get("event_type"))
            + " "
            + _as_text(item.get("title"))
            + " "
            + _as_text(item.get("summary"))
            + " "
            + _as_text(item.get("event_direction"))
        ).lower()
        for item in _ensure_list(events)
        if isinstance(item, dict)
    )


def _brief_root(config: Dict[str, Any]) -> Path:
    """研究计划输出目录。"""
    return ensure_dir(Path(str(config["paths"]["research_root"])) / "briefs")


def _save_research_brief_diagnostic(config: Dict[str, Any], payload: Dict[str, Any]) -> Path:
    """保存研究计划诊断信息。"""
    out_path = _brief_root(config) / "research_brief_diagnostic.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _compact_priority_events(events: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
    """抽取最小事件证据卡。"""
    compact: List[Dict[str, Any]] = []
    for item in _ensure_list(events)[:max_items]:
        if not isinstance(item, dict):
            continue
        title = _as_text(item.get("title") or item.get("raw_title"))[:120]
        event_type = _as_text(item.get("event_type") or item.get("structured_facts", {}).get("ollama_event_type") or "其他")[:40]
        summary = _as_text(item.get("summary") or item.get("structured_facts", {}).get("ollama_summary"))[:120]
        importance_raw = item.get("importance")
        if importance_raw is None:
            importance_raw = item.get("structured_facts", {}).get("ollama_importance")
        if importance_raw is None:
            importance_raw = round(float(item.get("importance_score", 0.0) or 0.0) * 10)
        try:
            importance = int(round(float(importance_raw or 0)))
        except Exception:
            importance = 0
        compact.append({
            "title": title,
            "event_type": event_type or "其他",
            "importance": importance,
            "summary": summary or title,
            "source_type": _as_text(item.get("source_type"))[:24],
            "event_direction": _as_text(item.get("event_direction") or "uncertain")[:20],
            "impact_scope": _as_text(item.get("impact_scope") or item.get("structured_facts", {}).get("impact_scope"))[:20],
            "impact_horizon": _as_text(item.get("impact_horizon") or item.get("structured_facts", {}).get("impact_horizon"))[:20],
            "evidence_quality": round(
                _priority_event_metric(item, "evidence_quality", _priority_event_metric(item, "evidence_quality_score")),
                4,
            ),
            "research_priority": round(
                _priority_event_metric(item, "research_priority", _priority_event_metric(item, "research_priority_score")),
                4,
            ),
            "corroboration_count": _priority_event_int(item, "corroboration_count", 1),
            "source_diversity": _priority_event_int(item, "source_diversity", 1),
            "anti_overfit_weight": round(_priority_event_metric(item, "anti_overfit_weight", 1.0), 4),
        })
    return compact


def _compact_data_gap_report(report: Dict[str, Any], slim: bool = False) -> Dict[str, Any]:
    """压缩数据缺口报告。"""
    report = dict(report or {})
    dataset_cards: List[Dict[str, Any]] = []
    for item in _ensure_list(report.get("datasets", [])):
        if not isinstance(item, dict):
            continue
        action = _as_text(item.get("action") or "none")
        freshness = _as_text(item.get("freshness_status") or "unknown")
        missing_ratio = float(item.get("missing_ratio", 0.0) or 0.0)
        if slim and action == "none" and freshness == "fresh" and missing_ratio <= 0:
            continue
        dataset_cards.append({
            "dataset_name": _as_text(item.get("dataset_name")),
            "freshness_status": freshness,
            "missing_ratio": round(missing_ratio, 4),
            "action": action,
        })

    refresh_tasks = []
    for item in _ensure_list(report.get("refresh_tasks", []))[:6]:
        if isinstance(item, dict):
            refresh_tasks.append({
                "task_id": _as_text(item.get("task_id")),
                "dataset_name": _as_text(item.get("dataset_name")),
                "priority": _as_text(item.get("priority") or "medium"),
                "reason": _as_text(item.get("reason")),
            })

    recompute_tasks = []
    for item in _ensure_list(report.get("recompute_tasks", []))[:6]:
        if isinstance(item, dict):
            recompute_tasks.append({
                "task_id": _as_text(item.get("task_id")),
                "task_type": _as_text(item.get("task_type")),
                "priority": _as_text(item.get("priority") or "medium"),
                "feature_name": _as_text(item.get("feature_name")),
                "reason": _as_text(item.get("reason")),
            })

    new_feature_candidates = []
    for item in _ensure_list(report.get("new_feature_candidates", []))[:8]:
        if isinstance(item, dict):
            new_feature_candidates.append({
                "feature_name": _as_text(item.get("feature_name")),
                "priority": _as_text(item.get("priority") or "medium"),
                "reason": _as_text(item.get("reason")),
            })

    return {
        "generated_at": _as_text(report.get("generated_at")),
        "summary": _as_text(report.get("summary")),
        "datasets": dataset_cards[:8],
        "refresh_tasks": refresh_tasks,
        "recompute_tasks": recompute_tasks,
        "new_feature_candidates": new_feature_candidates,
    }


def _compact_context_for_llm(context_pack: Dict[str, Any], max_priority_events: int = 12, slim: bool = False) -> Dict[str, Any]:
    """把上下文压成适合研究脑的最小证据包。"""
    return {
        "generated_at": _as_text(context_pack.get("generated_at")),
        "event_summary": dict(context_pack.get("event_summary", {}) or {}),
        "message_evidence_profile": dict(context_pack.get("message_evidence_profile", {}) or {}),
        "priority_events": _compact_priority_events(
            context_pack.get("priority_events") or context_pack.get("compact_priority_events") or [],
            max_items=max_priority_events,
        ),
        "evidence_cards": list(context_pack.get("evidence_cards", []) or [])[:4],
        "data_gap_report": _compact_data_gap_report(dict(context_pack.get("data_gap_report", {}) or {}), slim=slim),
        "market_state": dict(context_pack.get("market_state", {}) or {}),
        "industry_router": dict(context_pack.get("industry_router", {}) or {}),
        "integrated_thesis": dict(context_pack.get("integrated_thesis", {}) or {}),
        "recent_experiments": list(context_pack.get("recent_experiments", []) or [])[:6],
        "family_state": dict(context_pack.get("family_state", {}) or {}),
        "research_space": dict(context_pack.get("research_space", {}) or {}),
    }


def _build_prompt_contract() -> str:
    """研究 brief 的输出契约。"""
    return (
        "\n\n## 输出契约\n"
        "- 全部字段都尽量短句输出，单个字符串优先控制在 60 个中文字符以内。\n"
        "- research_thesis: 一句话总结今天研究主线。\n"
        "- core_theses: 优先输出 3 个对象，每个对象必须含 thesis_id, title, hypothesis, why_now, required_features, target_labels, route_bias, priority。\n"
        "- priority_events: 优先输出最关键的 5 个对象，每个对象必须含 title, event_type, importance, summary, research_angle。\n"
        "- data_actions / feature_actions / label_actions / model_actions / portfolio_actions / risk_actions: 都输出对象数组，必须写 action / priority / reason，避免长段落。\n"
        "- candidate_experiments: 优先输出 6 个对象，每个对象必须含 experiment_id, name, hypothesis, route, features, models, labels, top_k, reason；features 不超过 4 个，models 不超过 2 个，labels 不超过 2 个。\n"
        "- stop_conditions 与 ban_items 必须是字符串数组。\n"
        "- 只输出一个合法 JSON 对象，不要输出 markdown，不要输出代码块。"
    )


def _infer_route(text: str) -> str:
    """从文本推断 route。"""
    lower = text.lower()
    if any(x in lower for x in ["risk", "drawdown", "诉讼", "仲裁", "风险", "监管"]):
        return "risk"
    if any(x in lower for x in ["portfolio", "持仓", "仓位", "组合", "top_k"]):
        return "portfolio"
    if any(x in lower for x in ["model", "模型", "ranker", "xgboost", "lightgbm"]):
        return "model"
    if any(x in lower for x in ["train", "training", "sample_weight", "权重", "正则"]):
        return "training"
    if any(x in lower for x in ["data", "refresh", "补数据", "刷新", "重算"]):
        return "data"
    if any(x in lower for x in ["hybrid", "联合", "联动"]):
        return "hybrid"
    return "feature"


def _summarize_research_thesis(core_theses: List[Dict[str, Any]], data_gap_report: Dict[str, Any]) -> str:
    """生成简洁研究主线摘要。"""
    titles = [_as_text(item.get("title")) for item in core_theses if _as_text(item.get("title"))]
    titles = titles[:2]
    if titles:
        summary = "；".join(titles)
        if data_gap_report.get("recompute_tasks"):
            return f"围绕 {summary}，优先补齐事件型特征重算与组合验证。"
        return f"围绕 {summary}，优先做事件驱动 alpha 与风控联动验证。"
    return "围绕高重要性事件做事件驱动 alpha、数据补齐与组合风控联动验证。"


def _derive_default_feature_names(priority_events: List[Dict[str, Any]], data_gap_report: Dict[str, Any]) -> List[str]:
    """从证据包推导默认特征名。"""
    features: List[str] = []
    for item in _ensure_list(data_gap_report.get("recompute_tasks", [])):
        if isinstance(item, dict) and _as_text(item.get("feature_name")):
            features.append(_as_text(item.get("feature_name")))
    for item in _ensure_list(data_gap_report.get("new_feature_candidates", [])):
        if isinstance(item, dict) and _as_text(item.get("feature_name")):
            features.append(_as_text(item.get("feature_name")))

    qualified_events = _qualified_priority_events(priority_events)
    event_blob = _event_blob(qualified_events or priority_events)
    features.extend(["event_quality_weighted_density_20d", "confirmed_event_ratio_20d"])
    if not qualified_events:
        features.extend(["headline_noise_filter_20d", "source_diversity_score_20d"])
    if any(x in event_blob for x in ["业绩", "财务", "earnings", "financial_report", "earnings_preannounce", "earnings_flash"]):
        features.extend(["earnings_surprise_proxy", "earnings_event_density_20d"])
    if any(x in event_blob for x in ["诉讼", "仲裁", "监管", "risk", "处罚", "litigation_arbitration", "regulatory_action", "risk_warning"]):
        features.extend(["negative_event_pressure_20d", "regulatory_event_density_20d"])
    if any(x in event_blob for x in ["回购", "增持", "减持", "management_trade", "buyback_dividend", "capital_flow_event"]):
        features.extend(["capital_action_balance_20d", "shareholder_trade_pressure_20d"])
    if any(x in event_blob for x in ["重组", "并购", "收购", "重大合同", "中标", "mna_restructure", "major_contract"]):
        features.extend(["corporate_action_quality_20d", "event_confirmation_strength_20d"])
    if any(x in event_blob for x in ["政策", "行业", "板块", "policy_industry_event"]):
        features.extend(["policy_event_strength_20d", "industry_event_diffusion_20d"])
    return list(dict.fromkeys([x for x in features if x]))[:8]


def _build_rule_based_recovery_brief(context_pack: Dict[str, Any], generation_mode: str) -> Dict[str, Any]:
    """基于证据包生成真实、可执行的研究计划。"""
    compact_context = _compact_context_for_llm(context_pack=context_pack, max_priority_events=8, slim=True)
    priority_events = compact_context.get("priority_events", [])
    data_gap_report = dict(compact_context.get("data_gap_report", {}) or {})
    event_summary = dict(compact_context.get("event_summary", {}) or {})
    message_evidence_profile = dict(compact_context.get("message_evidence_profile", {}) or {})
    research_space = dict(compact_context.get("research_space", {}) or {})
    feature_pool = _derive_default_feature_names(priority_events, data_gap_report)
    qualified_events = _qualified_priority_events(priority_events)

    high_count = int(event_summary.get("high_importance_events", 0) or 0)
    high_quality_count = int(event_summary.get("high_quality_events", 0) or 0)
    confirmed_count = int(event_summary.get("confirmed_events", 0) or 0)
    weak_signal_count = int(event_summary.get("weak_signal_events", 0) or 0)
    total_events = int(event_summary.get("total_events", 0) or 0)
    confirmed_ratio = _float_value(message_evidence_profile.get("confirmed_ratio"), 0.0)
    weak_signal_ratio = _float_value(message_evidence_profile.get("weak_signal_ratio"), 0.0)
    message_profile = _as_text(message_evidence_profile.get("profile") or "mixed")
    qualified_blob = _event_blob(qualified_events)
    event_blob = _event_blob(priority_events)

    evidence_assessment = "mixed_quality"
    if qualified_events and confirmed_ratio >= 0.50 and high_quality_count >= 2:
        evidence_assessment = "evidence_backed"
    elif not qualified_events or (weak_signal_ratio >= 0.55 and confirmed_ratio < 0.35):
        evidence_assessment = "weak_message_noise"

    core_theses: List[Dict[str, Any]] = []
    if evidence_assessment == "weak_message_noise":
        core_theses.append({
            "thesis_id": "T1",
            "title": "先做消息质量分层，再决定哪些事件值得进研究主线",
            "hypothesis": "当前消息面里弱证据或标题级噪声占比偏高，先做确认度和质量权重过滤，比直接追标题更稳健。",
            "why_now": f"当前弱信号事件数={weak_signal_count}，高质量事件数={high_quality_count}，需要先防止消息面对研究脑过拟合。",
            "required_features": list(dict.fromkeys(feature_pool + ["headline_noise_filter_20d", "source_diversity_score_20d"]))[:6],
            "target_labels": ["future_ret_5d", "future_ret_10d"],
            "route_bias": ["data", "feature", "risk"],
            "priority": "P0",
        })
    else:
        core_theses.append({
            "thesis_id": "T2",
            "title": "消息面对研究计划的影响应由高质量、可确认事件来驱动",
            "hypothesis": "只有被确认且具备实体指向的事件，才值得转成横截面特征和组合实验主线。",
            "why_now": f"当前确认事件数={confirmed_count}，高质量事件数={high_quality_count}，具备做质量加权事件特征的基础。",
            "required_features": list(dict.fromkeys(feature_pool + ["event_quality_weighted_density_20d", "confirmed_event_ratio_20d"]))[:6],
            "target_labels": ["future_ret_5d", "future_ret_10d"],
            "route_bias": ["feature", "data", "portfolio"],
            "priority": "P0",
        })
    if any(x in qualified_blob or event_blob for x in ["业绩", "财务", "earnings", "financial_report", "earnings_preannounce", "earnings_flash"]) or data_gap_report.get("recompute_tasks"):
        core_theses.append({
            "thesis_id": "T3",
            "title": "财务业绩事件要用质量权重和确认度来做，而不是用标题关键词硬推",
            "hypothesis": "财务与业绩类事件在高质量过滤后，更可能形成可训练而非一次性标题驱动的 alpha。",
            "why_now": "当前事件池里存在财务业绩线索，适合把 earnings 特征放到质量过滤框架内验证。",
            "required_features": list(dict.fromkeys(feature_pool + ["earnings_surprise_proxy", "event_confirmation_strength_20d"]))[:6],
            "target_labels": ["future_ret_5d", "future_ret_10d"],
            "route_bias": ["feature", "model"],
            "priority": "P1",
        })
    if high_count >= 4 or confirmed_count >= 4:
        core_theses.append({
            "thesis_id": "T4",
            "title": "事件簇密度要按质量和确认度加权，不能把所有热闹都算成同样的信号",
            "hypothesis": "高质量事件簇比原始标题堆积更能解释后续 alpha 与风险分层。",
            "why_now": f"当前高重要性事件数={high_count}、确认事件数={confirmed_count}，适合验证质量加权的 event density。",
            "required_features": list(dict.fromkeys(feature_pool + ["event_quality_weighted_density_20d", "event_confirmation_strength_20d"]))[:6],
            "target_labels": ["future_ret_5d"],
            "route_bias": ["feature", "portfolio"],
            "priority": "P1",
        })
    if any(x in qualified_blob or event_blob for x in ["诉讼", "仲裁", "监管", "风险", "处罚", "litigation_arbitration", "regulatory_action", "risk_warning"]):
        core_theses.append({
            "thesis_id": "T5",
            "title": "负面事件压力需要单独进入风险分支，而不是只让收益模型吸收",
            "hypothesis": "诉讼、监管和风险提示这类负面事件，更适合进入 risk route 和组合约束层。",
            "why_now": "当前事件池包含负面治理/监管线索，适合单独验证 risk 与 portfolio 联动。",
            "required_features": list(dict.fromkeys(feature_pool + ["negative_event_pressure_20d", "regulatory_event_density_20d"]))[:6],
            "target_labels": ["future_ret_5d", "future_ret_10d"],
            "route_bias": ["risk", "portfolio"],
            "priority": "P1",
        })
    if any(x in qualified_blob or event_blob for x in ["回购", "增持", "减持", "management_trade", "buyback_dividend", "capital_flow_event"]):
        core_theses.append({
            "thesis_id": "T6",
            "title": "资本行为类事件更适合作为组合构建和风格偏置校正器",
            "hypothesis": "回购、增减持和限售解禁更可能通过组合构建层，而不是单点收益预测层，产生稳定增量。",
            "why_now": "当前消息面含资本行为事件，适合同步验证 portfolio route 和 feature route。",
            "required_features": list(dict.fromkeys(feature_pool + ["capital_action_balance_20d", "shareholder_trade_pressure_20d"]))[:6],
            "target_labels": ["future_ret_5d"],
            "route_bias": ["portfolio", "feature"],
            "priority": "P2",
        })
    if not core_theses:
        core_theses.append({
            "thesis_id": "T1",
            "title": "先把消息面做成稳健特征，再决定是否放大到研究主线",
            "hypothesis": "即使当日消息面不够强，也应该先验证确认度、质量权重和噪声过滤，而不是放弃消息层建设。",
            "why_now": f"当前总事件数={total_events}，但稳健证据不足，需要先做泛化更强的消息特征底座。",
            "required_features": feature_pool or ["event_quality_weighted_density_20d"],
            "target_labels": ["future_ret_5d", "future_ret_10d"],
            "route_bias": ["feature", "data"],
            "priority": "P0",
        })

    label_horizons = _int_list(research_space.get("label_horizons") or [5, 10])
    if not label_horizons:
        label_horizons = [5, 10]

    data_actions: List[Dict[str, Any]] = []
    for item in _ensure_list(data_gap_report.get("refresh_tasks", [])):
        if isinstance(item, dict):
            data_actions.append({
                "action": "refresh_dataset",
                "dataset_name": _as_text(item.get("dataset_name") or item.get("task_id")),
                "priority": _as_text(item.get("priority") or "medium"),
                "reason": _as_text(item.get("reason") or "数据新鲜度不足，需要先刷新。"),
            })
    if evidence_assessment != "evidence_backed":
        data_actions.append({
            "action": "review_message_quality_state",
            "dataset_name": "event_store",
            "priority": "high",
            "reason": "先确认消息面里有多少事件值得进入研究主线，避免被标题噪声拖偏。",
        })

    feature_actions: List[Dict[str, Any]] = []
    for item in _ensure_list(data_gap_report.get("recompute_tasks", [])):
        if isinstance(item, dict):
            feature_actions.append({
                "action": "recompute_feature",
                "feature_name": _as_text(item.get("feature_name") or item.get("task_id")),
                "priority": _as_text(item.get("priority") or "high"),
                "reason": _as_text(item.get("reason") or "由事件触发的特征重算任务。"),
                "source_dataset": "event_store",
            })
    for item in _ensure_list(data_gap_report.get("new_feature_candidates", [])):
        if isinstance(item, dict):
            feature_actions.append({
                "action": "build_feature",
                "feature_name": _as_text(item.get("feature_name")),
                "priority": _as_text(item.get("priority") or "high"),
                "reason": _as_text(item.get("reason") or "由 data_gap_report 推荐的新特征。"),
                "source_dataset": "event_store",
            })
    for name in (feature_pool[:3] or ["event_quality_weighted_density_20d", "confirmed_event_ratio_20d"]):
        feature_actions.append({
            "action": "build_feature",
            "feature_name": name,
            "priority": "high" if name in {"event_quality_weighted_density_20d", "confirmed_event_ratio_20d"} else "medium",
            "reason": "优先把高质量、可确认事件转成稳健特征，而不是围绕单条标题定制特征。",
            "source_dataset": "event_store",
        })
    if evidence_assessment == "weak_message_noise":
        feature_actions.append({
            "action": "build_feature",
            "feature_name": "headline_noise_filter_20d",
            "priority": "high",
            "reason": "消息面噪声偏高时，先把标题级噪声做成过滤器。",
            "source_dataset": "event_store",
        })
    feature_actions = feature_actions[:8]

    label_actions = [{
        "action": "validate_label_horizon",
        "label_horizon": int(h),
        "priority": "high" if int(h) <= 10 else "medium",
        "reason": "消息面研究先看 5D/10D 的稳定性，再决定是否扩大到更长周期。",
    } for h in label_horizons[:3]]

    model_actions = [
        {
            "action": "promote_model_family",
            "model_family": "xgboost_gpu",
            "priority": "high",
            "reason": "先用稳定 GPU 树模型承接质量加权后的消息特征和非线性交互。",
        },
        {
            "action": "keep_baseline",
            "model_family": "lightgbm_auto",
            "priority": "medium",
            "reason": "保留高效基线，避免消息层结论绑定单一模型族。",
        },
        {
            "action": "keep_baseline",
            "model_family": "ridge_ranker",
            "priority": "medium",
            "reason": "保留更朴素的基线，专门检查消息特征是否只是把噪声拟合进树模型。",
        },
    ]

    portfolio_actions = []
    if evidence_assessment == "weak_message_noise":
        portfolio_actions.append({
            "action": "test_top_k",
            "top_k": 20,
            "priority": "high",
            "reason": "消息证据偏弱时先看更分散组合，避免集中押注单条标题。",
        })
    portfolio_actions.append({
        "action": "test_top_k",
        "top_k": 10,
        "priority": "high" if evidence_assessment == "evidence_backed" else "medium",
        "reason": "确认事件足够强时，再看更集中的组合承载能力。",
    })
    portfolio_actions.append({
        "action": "test_top_k",
        "top_k": 20,
        "priority": "medium",
        "reason": "对比分散组合，判断消息 alpha 是否被组合构建层放大或吃掉。",
    })

    risk_actions = [{
        "action": "increase_risk_route_weight",
        "priority": "high" if any(x in event_blob for x in ["诉讼", "仲裁", "监管", "风险", "处罚"]) or evidence_assessment == "weak_message_noise" else "medium",
        "reason": "把负面消息和弱证据消息先交给 risk route 和过滤逻辑，而不是直接交给收益模型。",
    }]

    top_features = feature_pool or ["generated_feature_pack"]
    candidate_experiments = [
        {
            "experiment_id": "EXP1",
            "name": "message_quality_filter_ablation",
            "hypothesis": "消息质量过滤和确认度约束会比原始标题事件更稳健。",
            "route": "feature",
            "features": list(dict.fromkeys(top_features + ["headline_noise_filter_20d", "confirmed_event_ratio_20d"]))[:6],
            "models": ["xgboost_gpu"],
            "labels": [5],
            "top_k": 20 if evidence_assessment == "weak_message_noise" else 10,
            "reason": "先证明消息质量过滤本身能提供增量，再放大到更激进的事件主线。",
        },
        {
            "experiment_id": "EXP2",
            "name": "quality_weighted_event_density_lgbm_10d",
            "hypothesis": "质量加权的事件密度，比原始事件计数更适合 10D 视角。",
            "route": "data",
            "features": list(dict.fromkeys(top_features + ["event_quality_weighted_density_20d"]))[:6],
            "models": ["lightgbm_auto"],
            "labels": [10],
            "top_k": 20,
            "reason": "把消息层的新特征先放进更稳的中周期基线验证，不直接依赖短期情绪。",
        },
        {
            "experiment_id": "EXP3",
            "name": "negative_event_pressure_risk_branch",
            "hypothesis": "负面事件压力需要独立风险分支才能减少组合尾部回撤。",
            "route": "risk",
            "features": list(dict.fromkeys(top_features + ["negative_event_pressure_20d", "regulatory_event_density_20d"]))[:6],
            "models": ["xgboost_gpu"],
            "labels": [5, 10],
            "top_k": 20,
            "reason": "把负面消息显式纳入组合构建与仓位约束，而不是只在收益模型中隐式吸收。",
        },
        {
            "experiment_id": "EXP4",
            "name": "portfolio_topk_sensitivity",
            "hypothesis": "当前消息 alpha 可能存在，但被组合层的持仓数和集中度配置吃掉。",
            "route": "portfolio",
            "features": top_features[:6],
            "models": ["xgboost_gpu"],
            "labels": [5],
            "top_k": 10 if evidence_assessment == "evidence_backed" else 20,
            "reason": "优先验证 10/20 持仓数切换是否改变消息 alpha 的承载方式。",
        },
        {
            "experiment_id": "EXP5",
            "name": "model_family_cross_check",
            "hypothesis": "同一组消息特征在不同模型族上可能出现互补，也可能暴露出纯过拟合。",
            "route": "model",
            "features": top_features[:6],
            "models": ["xgboost_gpu", "lightgbm_auto", "ridge_ranker"],
            "labels": [5, 10],
            "top_k": 20,
            "reason": "防止消息结论绑定单一模型实现，顺手筛掉只在复杂模型上成立的假信号。",
        },
        {
            "experiment_id": "EXP6",
            "name": "confirmed_single_name_event_xgb",
            "hypothesis": "确认度高且有实体指向的事件，比泛化市场标题更适合转成横截面 alpha。",
            "route": "hybrid",
            "features": list(dict.fromkeys(top_features + ["confirmed_event_ratio_20d", "event_confirmation_strength_20d"]))[:6],
            "models": ["xgboost_gpu", "lightgbm_auto"],
            "labels": [5, 10],
            "top_k": 10,
            "reason": "只让确认度足够高的单名事件进入更激进的联动验证，避免泛化市场噪声主导实验空间。",
        },
    ]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generation_mode": generation_mode,
        "llm_provider": generation_mode,
        "llm_model": "",
        "selected_attempt": generation_mode,
        "research_thesis": _summarize_research_thesis(core_theses, data_gap_report),
        "core_theses": core_theses[:5],
        "why_now": (
            f"当前总事件数={total_events}、高重要性事件数={high_count}、高质量事件数={high_quality_count}、确认事件数={confirmed_count}，"
            f"消息证据画像={message_profile}，"
            f"且 data_gap_report 给出了 {len(_ensure_list(data_gap_report.get('recompute_tasks', [])))} 个重算任务和 "
            f"{len(_ensure_list(data_gap_report.get('new_feature_candidates', [])))} 个新特征候选。"
        ),
        "priority_events": priority_events[:8],
        "data_actions": data_actions,
        "feature_actions": feature_actions,
        "label_actions": label_actions,
        "model_actions": model_actions,
        "portfolio_actions": portfolio_actions,
        "risk_actions": risk_actions,
        "candidate_experiments": candidate_experiments,
        "today_change_research_direction": "从标题级消息追逐转向高质量、可确认事件驱动的特征与风控联动验证。",
        "today_fill_data": "优先执行消息质量过滤、确认度特征建设，以及 data_gap_report 标出的重算任务。",
        "priority_order_for_execution": [item["experiment_id"] for item in candidate_experiments],
        "new_feature_candidates": _ensure_list(data_gap_report.get("new_feature_candidates", []))[:8],
        "paused_branches": ["title_only_event_branch", "single_headline_alpha_branch"],
        "evidence_assessment": evidence_assessment,
        "stop_conditions": [
            "若连续三轮 candidate_experiments 的 valid/test 指标均无增量，则暂停当前消息主题。",
            "若消息质量过滤后仍无法改善 5D/10D 表现，则回退到只做数据建设与风险控制。",
            "若同一批消息特征只在单一模型族上有效，则判定为可疑过拟合并暂停扩展。",
        ],
        "ban_items": [
            "禁止把空壳 fallback brief 当作正式研究结论。",
            "禁止只改 bridge 而不验证 research_brief 是否来自真实事件证据。",
            "禁止围绕单条新闻标题定制专属特征名。",
            "禁止未确认的市场传闻直接进入 candidate_experiments。",
        ],
    }


def _normalize_priority_events(value: Any, default: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """规整优先事件列表。"""
    items: List[Dict[str, Any]] = []
    for item in _ensure_list(value):
        if isinstance(item, dict):
            title = _as_text(item.get("title"))
            if not title:
                continue
            importance_list = _int_list(item.get("importance"))
            items.append({
                "title": title[:120],
                "event_type": _as_text(item.get("event_type") or "其他")[:40],
                "importance": importance_list[0] if importance_list else 0,
                "summary": _as_text(item.get("summary") or title)[:120],
                "research_angle": _as_text(item.get("research_angle") or item.get("why_now") or "纳入今日研究优先级。")[:160],
                "source_type": _as_text(item.get("source_type"))[:24],
                "event_direction": _as_text(item.get("event_direction") or "uncertain")[:20],
                "impact_scope": _as_text(item.get("impact_scope"))[:20],
                "impact_horizon": _as_text(item.get("impact_horizon"))[:20],
                "evidence_quality": round(_priority_event_metric(item, "evidence_quality", _priority_event_metric(item, "evidence_quality_score")), 4),
                "research_priority": round(_priority_event_metric(item, "research_priority", _priority_event_metric(item, "research_priority_score")), 4),
                "corroboration_count": _priority_event_int(item, "corroboration_count", 1),
                "source_diversity": _priority_event_int(item, "source_diversity", 1),
                "anti_overfit_weight": round(_priority_event_metric(item, "anti_overfit_weight", 1.0), 4),
            })
        elif _as_text(item):
            text = _as_text(item)[:120]
            items.append({
                "title": text,
                "event_type": "其他",
                "importance": 0,
                "summary": text,
                "research_angle": "由模型直接提及，需进入今日研究候选。",
                "source_type": "",
                "event_direction": "uncertain",
                "impact_scope": "",
                "impact_horizon": "",
                "evidence_quality": 0.0,
                "research_priority": 0.0,
                "corroboration_count": 1,
                "source_diversity": 1,
                "anti_overfit_weight": 1.0,
            })
    return items[:8] or default


def _normalize_core_theses(value: Any, research_thesis_value: Any, default: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """规整核心主命题。"""
    raw_items = _ensure_list(value)
    if not raw_items and isinstance(research_thesis_value, list):
        raw_items = _ensure_list(research_thesis_value)

    theses: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            title = _as_text(item.get("title") or item.get("thesis") or item.get("summary") or item.get("hypothesis"))
            if not title:
                continue
            theses.append({
                "thesis_id": _as_text(item.get("thesis_id") or item.get("id") or f"T{idx}"),
                "title": title[:160],
                "hypothesis": _as_text(item.get("hypothesis") or title)[:200],
                "why_now": _as_text(item.get("why_now") or item.get("reason") or "由研究脑给出的主命题。")[:200],
                "required_features": _string_list(item.get("required_features") or item.get("features")),
                "target_labels": _string_list(item.get("target_labels") or item.get("labels")),
                "route_bias": _string_list(item.get("route_bias") or item.get("routes")),
                "priority": _as_text(item.get("priority") or "P1")[:12],
            })
        elif _as_text(item):
            text = _as_text(item)
            theses.append({
                "thesis_id": f"T{idx}",
                "title": text[:160],
                "hypothesis": text[:200],
                "why_now": "由研究脑直接输出的核心主命题。",
                "required_features": [],
                "target_labels": [],
                "route_bias": [_infer_route(text)],
                "priority": "P1",
            })

    if not theses and _as_text(research_thesis_value) and not isinstance(research_thesis_value, list):
        text = _as_text(research_thesis_value)
        theses.append({
            "thesis_id": "T1",
            "title": text[:160],
            "hypothesis": text[:200],
            "why_now": "由 research_thesis 主摘要展开。",
            "required_features": [],
            "target_labels": [],
            "route_bias": [_infer_route(text)],
            "priority": "P1",
        })

    return theses[:5] or default


def _normalize_action_list(value: Any, default: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """规整 action 列表。"""
    normalized: List[Dict[str, Any]] = []
    for item in _ensure_list(value):
        if isinstance(item, dict):
            base = {
                "action": _as_text(item.get("action") or item.get("name") or item.get("task_type") or "act"),
                "priority": _as_text(item.get("priority") or "medium"),
                "reason": _as_text(item.get("reason") or item.get("why") or "由研究脑给出的行动项。"),
            }
            if kind == "data":
                base["dataset_name"] = _as_text(item.get("dataset_name") or item.get("task_id") or item.get("target"))
            elif kind == "feature":
                base["feature_name"] = _as_text(item.get("feature_name") or item.get("name") or item.get("target"))
                base["source_dataset"] = _as_text(item.get("source_dataset") or "event_store")
            elif kind == "label":
                horizons = _int_list(item.get("label_horizon") or item.get("labels") or item.get("target_labels"))
                base["label_horizon"] = horizons[0] if horizons else 5
            elif kind == "model":
                base["model_family"] = _as_text(item.get("model_family") or item.get("model") or item.get("name") or "xgboost_gpu")
            elif kind == "portfolio":
                top_ks = _int_list(item.get("top_k") or item.get("top_ks"))
                base["top_k"] = top_ks[0] if top_ks else 10
            normalized.append(base)
        elif _as_text(item):
            text = _as_text(item)
            base = {
                "action": text[:80],
                "priority": "medium",
                "reason": text[:200],
            }
            if kind == "data":
                base["dataset_name"] = "event_store"
            elif kind == "feature":
                base["feature_name"] = text[:80]
                base["source_dataset"] = "event_store"
            elif kind == "label":
                horizons = _int_list(text)
                base["label_horizon"] = horizons[0] if horizons else 5
            elif kind == "model":
                base["model_family"] = "xgboost_gpu"
            elif kind == "portfolio":
                top_ks = _int_list(text)
                base["top_k"] = top_ks[0] if top_ks else 10
            normalized.append(base)
    return normalized[:8] or default


def _normalize_candidate_experiments(value: Any, default: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """规整候选实验。"""
    experiments: List[Dict[str, Any]] = []
    allowed_routes = {"feature", "data", "risk", "portfolio", "training", "model", "hybrid"}
    for idx, item in enumerate(_ensure_list(value), start=1):
        if isinstance(item, dict):
            text_blob = " ".join(_string_list([
                item.get("name"),
                item.get("hypothesis"),
                item.get("reason"),
                item.get("route"),
            ], max_items=8))
            labels = _int_list(item.get("labels") or item.get("label_horizons") or item.get("target_labels"))
            top_ks = _int_list(item.get("top_k") or item.get("top_ks"))
            route_text = _as_text(item.get("route") or item.get("research_route"))
            normalized_route = route_text if route_text in allowed_routes else _infer_route(text_blob + " " + route_text)
            experiments.append({
                "experiment_id": _as_text(item.get("experiment_id") or item.get("id") or f"EXP{idx}"),
                "name": _as_text(item.get("name") or item.get("title") or f"candidate_{idx}")[:120],
                "hypothesis": _as_text(item.get("hypothesis") or item.get("title") or item.get("reason") or "验证研究脑提出的实验命题。")[:220],
                "route": normalized_route,
                "features": _string_list(item.get("features") or item.get("required_features"), max_items=8),
                "models": _string_list(item.get("models") or item.get("model_families") or item.get("preferred_models"), max_items=4),
                "labels": labels[:3] or [5],
                "top_k": top_ks[0] if top_ks else 10,
                "reason": _as_text(item.get("reason") or item.get("why_now") or item.get("hypothesis") or "由研究脑生成的候选实验。")[:220],
            })
        elif _as_text(item):
            text = _as_text(item)
            experiments.append({
                "experiment_id": f"EXP{idx}",
                "name": text[:120],
                "hypothesis": text[:220],
                "route": _infer_route(text),
                "features": [],
                "models": ["xgboost_gpu"],
                "labels": [5],
                "top_k": 10,
                "reason": text[:220],
            })
    return experiments[:12] or default


def _merge_with_payload(
    payload: Dict[str, Any],
    recovery_brief: Dict[str, Any],
    generation_mode: str,
    llm_provider: str = "",
    llm_model: str = "",
    selected_attempt: str = "",
) -> Dict[str, Any]:
    """把模型输出合并回证据驱动骨架。"""
    brief = dict(recovery_brief)
    research_thesis_value = payload.get("research_thesis", "")
    core_theses = _normalize_core_theses(
        value=payload.get("core_theses"),
        research_thesis_value=research_thesis_value,
        default=list(recovery_brief.get("core_theses", [])),
    )
    research_thesis_text = _as_text(research_thesis_value)
    if not research_thesis_text and isinstance(research_thesis_value, list):
        research_thesis_text = _summarize_research_thesis(core_theses, dict(recovery_brief.get("data_gap_report", {}) or {}))
    if not research_thesis_text:
        research_thesis_text = _summarize_research_thesis(core_theses, dict(recovery_brief.get("data_gap_report", {}) or {}))

    brief.update({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generation_mode": generation_mode,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "selected_attempt": selected_attempt,
        "research_thesis": research_thesis_text,
        "core_theses": core_theses,
        "why_now": _as_text(payload.get("why_now")) or _as_text(recovery_brief.get("why_now")),
        "priority_events": _normalize_priority_events(payload.get("priority_events"), list(recovery_brief.get("priority_events", []))),
        "data_actions": _normalize_action_list(payload.get("data_actions"), list(recovery_brief.get("data_actions", [])), kind="data"),
        "feature_actions": _normalize_action_list(payload.get("feature_actions"), list(recovery_brief.get("feature_actions", [])), kind="feature"),
        "label_actions": _normalize_action_list(payload.get("label_actions"), list(recovery_brief.get("label_actions", [])), kind="label"),
        "model_actions": _normalize_action_list(payload.get("model_actions"), list(recovery_brief.get("model_actions", [])), kind="model"),
        "portfolio_actions": _normalize_action_list(payload.get("portfolio_actions"), list(recovery_brief.get("portfolio_actions", [])), kind="portfolio"),
        "risk_actions": _normalize_action_list(payload.get("risk_actions"), list(recovery_brief.get("risk_actions", [])), kind="risk"),
        "candidate_experiments": _normalize_candidate_experiments(payload.get("candidate_experiments"), list(recovery_brief.get("candidate_experiments", []))),
        "today_change_research_direction": _as_text(payload.get("today_change_research_direction")) or _as_text(recovery_brief.get("today_change_research_direction")),
        "today_fill_data": _as_text(payload.get("today_fill_data")) or _as_text(recovery_brief.get("today_fill_data")),
        "priority_order_for_execution": _string_list(payload.get("priority_order_for_execution") or recovery_brief.get("priority_order_for_execution"), max_items=12),
        "new_feature_candidates": _ensure_list(payload.get("new_feature_candidates") or recovery_brief.get("new_feature_candidates", []))[:8],
        "paused_branches": _string_list(payload.get("paused_branches") or recovery_brief.get("paused_branches"), max_items=8),
        "evidence_assessment": _as_text(payload.get("evidence_assessment")) or _as_text(recovery_brief.get("evidence_assessment")),
        "stop_conditions": _string_list(payload.get("stop_conditions") or recovery_brief.get("stop_conditions"), max_items=8),
        "ban_items": _string_list(payload.get("ban_items") or recovery_brief.get("ban_items"), max_items=8),
    })
    return brief


def _is_llm_payload_useful(payload: Dict[str, Any]) -> bool:
    """判断模型输出是否有研究价值。"""
    if not isinstance(payload, dict) or not payload:
        return False
    if _ensure_list(payload.get("candidate_experiments")):
        return True
    if _ensure_list(payload.get("core_theses")):
        return True
    if _ensure_list(payload.get("priority_events")):
        return True
    research_thesis = payload.get("research_thesis")
    if isinstance(research_thesis, list) and research_thesis:
        return True
    if _as_text(research_thesis):
        return True
    return False


def _dedupe_strings(values: List[str]) -> List[str]:
    """去重并清理字符串列表。"""
    cleaned: List[str] = []
    for value in values:
        text = _as_text(value)
        if text:
            cleaned.append(text)
    return list(dict.fromkeys(cleaned))


def _build_research_attempts(config: Dict[str, Any], context_pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    """构建研究脑级联尝试序列。"""
    openai_cfg = dict(config.get("providers", {}).get("openai_research", {}) or {})
    deepseek_cfg = dict(config.get("providers", {}).get("deepseek_worker", {}) or {})
    ollama_cfg = dict(config.get("local_ollama", {}) or {})

    compact_primary = _compact_context_for_llm(context_pack=context_pack, max_priority_events=8, slim=True)
    compact_retry = _compact_context_for_llm(context_pack=context_pack, max_priority_events=6, slim=True)
    compact_worker = _compact_context_for_llm(context_pack=context_pack, max_priority_events=5, slim=True)

    attempts: List[Dict[str, Any]] = []
    openai_models = _dedupe_strings([openai_cfg.get("model", "")] + list(openai_cfg.get("fallback_models", []) or []))
    for idx, model_name in enumerate(openai_models, start=1):
        lower_name = model_name.lower()
        reasoning_effort = "low" if lower_name.startswith(("gpt-5", "o1", "o3", "o4")) else ""
        attempts.append({
            "name": f"openai_{idx:02d}_{model_name.replace('.', '_').replace('-', '_')}",
            "backend": "openai",
            "provider": "openai_research",
            "model": model_name,
            "schema_name": "research_brief.schema.json",
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": min(int(openai_cfg.get("timeout_seconds", 180) or 180), 150 if idx == 1 else 180),
            "max_output_tokens": 4200 if idx == 1 else 3200,
            "context_pack": compact_primary if idx == 1 else compact_retry,
        })

    deepseek_models = _dedupe_strings(list(deepseek_cfg.get("research_models", []) or [deepseek_cfg.get("model", "")]))
    for idx, model_name in enumerate(deepseek_models, start=1):
        attempts.append({
            "name": f"deepseek_{idx:02d}_{model_name.replace('.', '_').replace('-', '_')}",
            "backend": "deepseek",
            "provider": "deepseek_worker",
            "model": model_name,
            "schema_name": None,
            "reasoning_effort": "",
            "timeout_seconds": int(deepseek_cfg.get("timeout_seconds", 90) or 90),
            "max_output_tokens": 0,
            "context_pack": compact_retry,
        })
        attempts.append({
            "name": f"deepseek_retry_{idx:02d}_{model_name.replace('.', '_').replace('-', '_')}",
            "backend": "deepseek",
            "provider": "deepseek_worker",
            "model": model_name,
            "schema_name": None,
            "reasoning_effort": "",
            "timeout_seconds": int(deepseek_cfg.get("timeout_seconds", 90) or 90),
            "max_output_tokens": 0,
            "context_pack": compact_worker,
        })

    if bool(ollama_cfg.get("research_enabled", False)):
        ollama_models = _dedupe_strings(list(ollama_cfg.get("research_models", []) or [ollama_cfg.get("model", "")]))
        for idx, model_name in enumerate(ollama_models, start=1):
            attempts.append({
                "name": f"ollama_{idx:02d}_{model_name.replace('.', '_').replace('-', '_').replace(':', '_')}",
                "backend": "local_ollama",
                "provider": "local_ollama_research",
                "model": model_name,
                "schema_name": None,
                "reasoning_effort": "",
                "timeout_seconds": int(ollama_cfg.get("research_timeout_seconds", ollama_cfg.get("timeout_seconds", 120)) or 120),
                "max_output_tokens": 0,
                "context_pack": compact_retry,
            })

    return attempts


def _run_research_attempt(
    router: LLMRouter,
    attempt: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
) -> Dict[str, Any]:
    """执行单次研究脑尝试。"""
    backend = _as_text(attempt.get("backend"))
    if backend == "openai":
        return router.call_research_json_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=attempt.get("schema_name"),
            model_override=attempt.get("model"),
            timeout_seconds=int(attempt.get("timeout_seconds", 180) or 180),
            reasoning_effort=_as_text(attempt.get("reasoning_effort") or "low"),
            max_output_tokens=int(attempt.get("max_output_tokens", 0) or 0),
        )
    if backend == "deepseek":
        return router.call_worker_json_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=attempt.get("model"),
            timeout_seconds=int(attempt.get("timeout_seconds", 90) or 90),
        )
    return router.call_local_json_detailed(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model_override=attempt.get("model"),
        timeout_seconds=int(attempt.get("timeout_seconds", 120) or 120),
    )


def build_research_brief(
    config: Dict[str, Any],
    context_pack: Dict[str, Any],
    prompt_root: Path,
) -> Dict[str, Any]:
    """调用研究脑生成研究计划，失败时退回证据驱动恢复版。"""
    project_root = prompt_root.parent
    schema_root = project_root / "schemas"
    router = LLMRouter(
        provider_cfg=config.get("providers", {}),
        schema_root=schema_root,
        local_ollama_cfg=config.get("local_ollama", {}),
    )
    system_prompt = load_prompt(prompt_root / "gpt54_research_brief_system.txt")
    user_prompt_template = load_prompt(prompt_root / "gpt54_research_brief_user_template.md")
    recovery_brief = _build_rule_based_recovery_brief(context_pack=context_pack, generation_mode="rule_based_recovery")
    attempts = _build_research_attempts(config=config, context_pack=context_pack)

    diagnostics: Dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected_attempt": "",
        "selected_provider": "",
        "selected_model": "",
        "result": "rule_based_recovery",
        "attempts": [],
    }

    for attempt in attempts:
        compact_context = dict(attempt["context_pack"])
        user_prompt = user_prompt_template.replace(
            "{{research_context_pack_json}}",
            json.dumps(compact_context, ensure_ascii=False, indent=2),
        )
        user_prompt += _build_prompt_contract()

        result = _run_research_attempt(
            router=router,
            attempt=attempt,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        diagnostics["attempts"].append({
            "name": attempt["name"],
            "backend": attempt.get("backend"),
            "provider": attempt.get("provider"),
            "schema_name": attempt["schema_name"] or "",
            "reasoning_effort": attempt["reasoning_effort"],
            "timeout_seconds": int(attempt["timeout_seconds"]),
            "max_output_tokens": int(attempt["max_output_tokens"]),
            "context_chars": len(json.dumps(compact_context, ensure_ascii=False, indent=2)),
            "ok": bool(result.get("ok", False)),
            "status_code": result.get("status_code"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "error_type": _as_text(result.get("error_type")),
            "error_message": _as_text(result.get("error_message"))[:1200],
            "response_id": _as_text(result.get("response_id")),
            "requested_model": _as_text(attempt.get("model")),
            "resolved_model": _as_text(result.get("model")),
            "output_chars": int(result.get("output_chars", 0) or 0),
        })

        payload = result.get("data", {})
        if _is_llm_payload_useful(payload):
            provider_name = _as_text(result.get("provider") or attempt.get("provider"))
            model_name = _as_text(result.get("model") or attempt.get("model"))
            generation_mode = provider_name if provider_name else _as_text(attempt.get("backend")) or "llm_research"
            brief = _merge_with_payload(
                payload=dict(payload or {}),
                recovery_brief=recovery_brief,
                generation_mode=generation_mode,
                llm_provider=provider_name,
                llm_model=model_name,
                selected_attempt=_as_text(attempt.get("name")),
            )
            diagnostics["selected_attempt"] = attempt["name"]
            diagnostics["selected_provider"] = provider_name
            diagnostics["selected_model"] = model_name
            diagnostics["result"] = generation_mode
            _save_research_brief_diagnostic(config=config, payload=diagnostics)
            return brief

    diagnostics["selected_attempt"] = "rule_based_recovery"
    diagnostics["selected_provider"] = "rule_based_recovery"
    diagnostics["selected_model"] = ""
    _save_research_brief_diagnostic(config=config, payload=diagnostics)
    return recovery_brief


def save_research_brief(config: Dict[str, Any], brief: Dict[str, Any]) -> Path:
    """保存研究计划。"""
    out_path = _brief_root(config) / "research_brief.json"
    out_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
