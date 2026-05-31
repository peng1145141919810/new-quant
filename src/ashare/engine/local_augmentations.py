# -*- coding: utf-8 -*-
"""Additive local-LLM enhancements for observability and side artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests

from .config_utils import ensure_dir
from .llm_router import LocalOllamaChatClient
from .logging_utils import log_line


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_list(value: Any, max_items: int = 6, max_chars: int = 120) -> List[str]:
    out: List[str] = []
    for item in _ensure_list(value):
        text = _safe_text(item)
        if text and text not in out:
            out.append(text[:max_chars])
    return out[:max_items]


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_json_array(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(payload) if isinstance(payload, list) else []


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _append_json_array(path: Path, item: Dict[str, Any], max_items: int = 64) -> Path:
    payload = _load_json_array(path)
    payload.append(dict(item))
    _write_json(path, payload[-max_items:])
    return path


def _local_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("local_ollama", {}) or {})


_OLLAMA_HEALTH_CACHE: Dict[str, Dict[str, Any]] = {}


def _ollama_ready(config: Dict[str, Any]) -> bool:
    cfg = _local_cfg(config)
    base_url = str(cfg.get("base_url", "http://localhost:11434")).rstrip("/")
    now_ts = datetime.now().timestamp()
    cached = dict(_OLLAMA_HEALTH_CACHE.get(base_url, {}) or {})
    if cached and float(cached.get("expires_at", 0.0) or 0.0) >= now_ts:
        return bool(cached.get("ok", False))
    ok = False
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=2.0)
        ok = resp.ok
    except Exception:
        ok = False
    _OLLAMA_HEALTH_CACHE[base_url] = {"ok": ok, "expires_at": now_ts + 30.0}
    return ok


def _role_enabled(config: Dict[str, Any], role_name: str) -> bool:
    cfg = _local_cfg(config)
    raw = cfg.get(f"{role_name}_enabled", True)
    return bool(raw)


def _role_model(config: Dict[str, Any], role_name: str, fallback: str) -> str:
    cfg = _local_cfg(config)
    return str(cfg.get(f"{role_name}_model") or fallback or "").strip()


def _role_timeout(config: Dict[str, Any], role_name: str, fallback: int) -> int:
    cfg = _local_cfg(config)
    try:
        return int(cfg.get(f"{role_name}_timeout_seconds", fallback) or fallback)
    except Exception:
        return int(fallback)


def _role_max_items(config: Dict[str, Any], role_name: str, fallback: int) -> int:
    cfg = _local_cfg(config)
    try:
        return max(0, int(cfg.get(f"{role_name}_max_items", fallback) or fallback))
    except Exception:
        return int(fallback)


def _runtime_explainer_stages(config: Dict[str, Any]) -> List[str]:
    cfg = _local_cfg(config)
    stages = _string_list(cfg.get("runtime_explainer_stages"), max_items=12, max_chars=64)
    return stages or ["v6_planning", "v5_gpu", "portfolio_recommendation", "execution_bridge"]


def _role_client(config: Dict[str, Any], role_name: str, model: str, timeout_seconds: int) -> LocalOllamaChatClient:
    cfg = _local_cfg(config)
    role_cfg = {
        "research_enabled": True,
        "base_url": str(cfg.get("base_url", "http://localhost:11434")).rstrip("/"),
        "model": model,
        "timeout_seconds": timeout_seconds,
        "research_timeout_seconds": timeout_seconds,
    }
    return LocalOllamaChatClient(provider_name=f"local_ollama_{role_name}", cfg=role_cfg)


def _call_role_json(
    config: Dict[str, Any],
    role_name: str,
    system_prompt: str,
    user_prompt: str,
    fallback_model: str,
    fallback_timeout_seconds: int,
) -> Dict[str, Any]:
    if not _role_enabled(config, role_name):
        return {
            "ok": False,
            "data": {},
            "error_type": "disabled",
            "error_message": "role_disabled",
            "status_code": None,
            "elapsed_seconds": 0.0,
            "provider": f"local_ollama_{role_name}",
            "model": "",
        }
    model = _role_model(config, role_name, fallback=fallback_model)
    timeout_seconds = _role_timeout(config, role_name, fallback=fallback_timeout_seconds)
    if not model:
        return {
            "ok": False,
            "data": {},
            "error_type": "config_error",
            "error_message": "missing_role_model",
            "status_code": None,
            "elapsed_seconds": 0.0,
            "provider": f"local_ollama_{role_name}",
            "model": "",
        }
    if not _ollama_ready(config):
        return {
            "ok": False,
            "data": {},
            "error_type": "service_unavailable",
            "error_message": "ollama_unreachable",
            "status_code": None,
            "elapsed_seconds": 0.0,
            "provider": f"local_ollama_{role_name}",
            "model": model,
        }
    client = _role_client(config=config, role_name=role_name, model=model, timeout_seconds=timeout_seconds)
    return client.chat_json_detailed(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model_override=model,
        timeout_seconds=timeout_seconds,
    )


def _research_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config["paths"]["research_root"])))


def _announcement_signal_type(title: str) -> str:
    text = _safe_text(title)
    if any(token in text for token in ["业绩预告", "业绩快报", "年度报告", "半年度报告", "一季度报告", "三季度报告"]):
        return "earnings"
    if any(token in text for token in ["回购", "分红", "增持", "减持"]):
        return "capital_action"
    if any(token in text for token in ["重大合同", "中标"]):
        return "contract"
    if any(token in text for token in ["处罚", "问询", "停牌", "复牌", "诉讼", "仲裁", "风险提示"]):
        return "risk"
    if any(token in text for token in ["并购", "重组", "收购", "要约收购"]):
        return "corporate_action"
    return "other"


def _normalize_evidence_card(item: Dict[str, Any], payload: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    security_code = _safe_text(item.get("security_code_hint"))
    company_name = _safe_text(item.get("company_name_hint"))
    signal_type = _safe_text(payload.get("signal_type")) or _announcement_signal_type(_safe_text(item.get("title")))
    signal_strength = _safe_text(payload.get("signal_strength")).lower()
    if signal_strength not in {"high", "medium", "low"}:
        signal_strength = "high" if item.get("pdf_local_path") else "medium"
    impact_scope = _safe_text(payload.get("impact_scope")).lower()
    if impact_scope not in {"single_name", "sector", "market"}:
        impact_scope = "single_name" if (security_code or company_name) else "sector"
    impact_horizon = _safe_text(payload.get("impact_horizon")).lower()
    if impact_horizon not in {"1_3d", "1_4w", "1_3m"}:
        impact_horizon = "1_3m" if signal_type in {"earnings", "corporate_action"} else "1_4w"
    key_points = _string_list(payload.get("key_points"), max_items=4, max_chars=120)
    if not key_points:
        key_points = [_safe_text(item.get("title"))[:120]]
    research_angles = _string_list(payload.get("research_angles"), max_items=4, max_chars=120)
    if not research_angles:
        research_angles = ["核查该公告能否转成事件型特征或组合约束。"]
    risk_flags = _string_list(payload.get("risk_flags"), max_items=4, max_chars=120)
    return {
        "title": _safe_text(item.get("title"))[:160],
        "publish_time": _safe_text(item.get("publish_time")),
        "source_name": _safe_text(item.get("source_name")),
        "security_code_hint": security_code,
        "company_name_hint": company_name,
        "pdf_local_path": _safe_text(item.get("pdf_local_path")),
        "content_chars": len(_safe_text(item.get("content"))),
        "signal_type": signal_type,
        "signal_strength": signal_strength,
        "impact_scope": impact_scope,
        "impact_horizon": impact_horizon,
        "key_points": key_points,
        "research_angles": research_angles,
        "risk_flags": risk_flags,
        "why_relevant": _safe_text(payload.get("why_relevant"))[:220] or "高价值公告已被补正文，适合转成研究证据卡。",
        "llm_ok": bool(result.get("ok", False)),
        "llm_model": _safe_text(result.get("model")),
        "llm_elapsed_seconds": float(result.get("elapsed_seconds", 0.0) or 0.0),
    }


def _evidence_candidates(config: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keywords = list(config.get("event_ingest", {}).get("high_value_title_keywords", []) or [])
    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for item in raw_items:
        if _safe_text(item.get("source_type")) != "announcement":
            continue
        title = _safe_text(item.get("title"))
        content = _safe_text(item.get("content"))
        has_pdf = bool(_safe_text(item.get("pdf_local_path")) or _safe_text(item.get("pdf_path")))
        if not has_pdf and len(content) < 240:
            continue
        score = 0
        if has_pdf:
            score += 3
        if len(content) >= 800:
            score += 2
        if any(keyword and keyword in title for keyword in keywords):
            score += 2
        if _safe_text(item.get("security_code_hint")) or _safe_text(item.get("company_name_hint")):
            score += 1
        if score <= 0:
            continue
        scored.append((score, len(content), item))
    scored.sort(key=lambda row: (row[0], row[1], _safe_text(row[2].get("publish_time"))), reverse=True)
    max_items = _role_max_items(config, "evidence_card", fallback=2)
    return [item for _, _, item in scored[:max_items]]


def build_announcement_evidence_cards(config: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = ensure_dir(_research_root(config) / "evidence_cards")
    out_path = root / "announcement_evidence_cards.json"
    selected = _evidence_candidates(config=config, raw_items=raw_items)
    if not selected:
        payload = {
            "generated_at": _now_text(),
            "enabled": _role_enabled(config, "evidence_card"),
            "selected_items": 0,
            "cards": [],
        }
        _write_json(out_path, payload)
        return {"ok": True, "selected_items": 0, "cards": [], "path": str(out_path)}

    log_line(config, f"V6: 本地公告证据卡增强开始 selected={len(selected)}")
    cards: List[Dict[str, Any]] = []
    for idx, item in enumerate(selected, start=1):
        excerpt = _safe_text(item.get("content"))[:2200]
        system_prompt = (
            "你是 A 股量化研究的公告证据卡整理器。"
            "只输出一个 JSON 对象，不要输出解释，不要输出 markdown。"
        )
        user_prompt = json.dumps(
            {
                "task": "将高价值公告整理成研究证据卡",
                "schema": {
                    "signal_type": "earnings/capital_action/contract/risk/corporate_action/other",
                    "signal_strength": "high|medium|low",
                    "impact_scope": "single_name|sector|market",
                    "impact_horizon": "1_3d|1_4w|1_3m",
                    "key_points": ["最多 4 条"],
                    "research_angles": ["最多 4 条"],
                    "risk_flags": ["最多 4 条"],
                    "why_relevant": "一句话说明为什么值得进研究证据包",
                },
                "announcement": {
                    "title": _safe_text(item.get("title")),
                    "publish_time": _safe_text(item.get("publish_time")),
                    "security_code_hint": _safe_text(item.get("security_code_hint")),
                    "company_name_hint": _safe_text(item.get("company_name_hint")),
                    "source_name": _safe_text(item.get("source_name")),
                    "content_excerpt": excerpt,
                },
            },
            ensure_ascii=False,
        )
        result = _call_role_json(
            config=config,
            role_name="evidence_card",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            fallback_model="deepseek-r1:14b",
            fallback_timeout_seconds=180,
        )
        card = _normalize_evidence_card(item=item, payload=dict(result.get("data", {}) or {}), result=result)
        cards.append(card)
        log_line(
            config,
            f"V6: 本地公告证据卡完成 item={idx}/{len(selected)} ok={card['llm_ok']} model={card['llm_model'] or 'fallback'}",
        )

    payload = {
        "generated_at": _now_text(),
        "enabled": _role_enabled(config, "evidence_card"),
        "selected_items": len(selected),
        "cards": cards,
    }
    _write_json(out_path, payload)
    log_line(config, f"V6: 本地公告证据卡增强完成 cards={len(cards)} path={out_path}")
    return {"ok": True, "selected_items": len(selected), "cards": cards, "path": str(out_path)}


def _review_candidates(config: Dict[str, Any], structured_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[tuple[float, float, Dict[str, Any]]] = []
    for item in structured_events:
        review_status = _safe_text(item.get("review_status"))
        extract_model = _safe_text(item.get("extract_model"))
        if review_status != "review_required" and "rule" not in extract_model:
            continue
        facts = dict(item.get("structured_facts", {}) or {})
        priority = float(item.get("importance_score", 0.0) or 0.0) + float(facts.get("research_priority_score", 0.0) or 0.0)
        confidence = float(item.get("confidence", 0.0) or 0.0)
        ranked.append((priority, -confidence, item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    max_items = _role_max_items(config, "review_router", fallback=6)
    return [item for _, _, item in ranked[:max_items]]


def _fallback_review_route(item: Dict[str, Any]) -> Dict[str, Any]:
    facts = dict(item.get("structured_facts", {}) or {})
    security_code = _safe_text(item.get("security_code"))
    company_name = _safe_text(item.get("company_name"))
    confidence = float(item.get("confidence", 0.0) or 0.0)
    extract_model = _safe_text(item.get("extract_model"))
    evidence_quality = float(facts.get("evidence_quality_score", 0.0) or 0.0)
    if not security_code and not company_name:
        bucket = "entity_check"
        action = "核对主体名称和证券代码"
    elif "rule" in extract_model:
        bucket = "event_type_check"
        action = "核对事件类别和方向"
    elif evidence_quality < 0.45:
        bucket = "noise_check"
        action = "判断是否属于标题噪声"
    else:
        bucket = "impact_check"
        action = "核对影响范围和持有期"
    if confidence < 0.35:
        priority = "high"
    elif confidence < 0.55:
        priority = "medium"
    else:
        priority = "low"
    return {
        "event_id": _safe_text(item.get("event_id")),
        "title": _safe_text(item.get("raw_title") or item.get("title"))[:160],
        "review_priority": priority,
        "review_bucket": bucket,
        "reason": f"confidence={confidence:.2f} evidence_quality={evidence_quality:.2f} extract_model={extract_model or 'unknown'}",
        "suggested_action": action,
    }


def build_manual_review_queue(config: Dict[str, Any], structured_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = ensure_dir(_research_root(config) / "extract_summary")
    out_path = root / "manual_review_queue.json"
    selected = _review_candidates(config=config, structured_events=structured_events)
    if not selected:
        payload = {
            "generated_at": _now_text(),
            "enabled": _role_enabled(config, "review_router"),
            "queue_size": 0,
            "review_queue": [],
        }
        _write_json(out_path, payload)
        return {"ok": True, "queue_size": 0, "path": str(out_path), "review_queue": []}

    compact_items = []
    for item in selected:
        facts = dict(item.get("structured_facts", {}) or {})
        compact_items.append(
            {
                "event_id": _safe_text(item.get("event_id")),
                "title": _safe_text(item.get("raw_title") or item.get("title"))[:160],
                "event_type": _safe_text(item.get("event_type")),
                "extract_model": _safe_text(item.get("extract_model")),
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "importance_score": float(item.get("importance_score", 0.0) or 0.0),
                "evidence_quality": float(facts.get("evidence_quality_score", 0.0) or 0.0),
                "anti_overfit_weight": float(facts.get("anti_overfit_weight", 0.0) or 0.0),
                "security_code": _safe_text(item.get("security_code")),
                "company_name": _safe_text(item.get("company_name")),
            }
        )

    log_line(config, f"V6: 人工复核分流开始 selected={len(compact_items)}")
    system_prompt = (
        "你是量化事件抽取后的人工复核分流器。"
        "只输出一个 JSON 对象，不要输出解释，不要输出 markdown。"
    )
    user_prompt = json.dumps(
        {
            "task": "为待复核事件生成简短的人工复核队列",
            "schema": {
                "review_queue": [
                    {
                        "event_id": "对应输入 event_id",
                        "review_priority": "high|medium|low",
                        "review_bucket": "noise_check|entity_check|event_type_check|impact_check|risk_check",
                        "reason": "一句话",
                        "suggested_action": "一句话",
                    }
                ]
            },
            "items": compact_items,
        },
        ensure_ascii=False,
    )
    result = _call_role_json(
        config=config,
        role_name="review_router",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        fallback_model="qwen2.5:7b",
        fallback_timeout_seconds=90,
    )
    payload_queue = _ensure_list(dict(result.get("data", {}) or {}).get("review_queue"))
    route_map: Dict[str, Dict[str, Any]] = {}
    for item in payload_queue:
        if not isinstance(item, dict):
            continue
        event_id = _safe_text(item.get("event_id"))
        if event_id:
            route_map[event_id] = item

    review_queue: List[Dict[str, Any]] = []
    for source in selected:
        event_id = _safe_text(source.get("event_id"))
        if event_id in route_map:
            raw = route_map[event_id]
            fallback = _fallback_review_route(source)
            review_queue.append(
                {
                    "event_id": event_id,
                    "title": _safe_text(source.get("raw_title") or source.get("title"))[:160],
                    "review_priority": _safe_text(raw.get("review_priority")).lower() or fallback["review_priority"],
                    "review_bucket": _safe_text(raw.get("review_bucket")).lower() or fallback["review_bucket"],
                    "reason": _safe_text(raw.get("reason"))[:200] or fallback["reason"],
                    "suggested_action": _safe_text(raw.get("suggested_action"))[:120] or fallback["suggested_action"],
                }
            )
        else:
            review_queue.append(_fallback_review_route(source))

    payload = {
        "generated_at": _now_text(),
        "enabled": _role_enabled(config, "review_router"),
        "queue_size": len(review_queue),
        "llm_ok": bool(result.get("ok", False)),
        "llm_model": _safe_text(result.get("model")),
        "review_queue": review_queue,
    }
    _write_json(out_path, payload)
    high_priority = sum(item.get("review_priority") == "high" for item in review_queue)
    log_line(config, f"V6: 人工复核分流完成 queue_size={len(review_queue)} high_priority={high_priority} path={out_path}")
    return {"ok": True, "queue_size": len(review_queue), "path": str(out_path), "review_queue": review_queue}


_RUNTIME_STAGE_WATCH = {
    "v6_planning": {
        "watch_files": [
            "data/event_lake/research/context_pack/research_context_pack.json",
            "data/event_lake/research/briefs/research_brief_diagnostic.json",
            "data/event_lake/research/briefs/research_brief.json",
        ],
        "risk_hint": "如果长时间无新日志，先看 research_brief_diagnostic.json 是否卡在上游模型调用。",
    },
    "v5_gpu": {
        "watch_files": [
            "data/research_hub/controller_state.json",
            "data/research_hub/registry/experiment_registry.csv",
            "data/research_hub/cycles/*/cycle_summary.json",
        ],
        "risk_hint": "V5 是长阶段，重点看 controller_state.json 是否推进到新 cycle。",
    },
    "portfolio_recommendation": {
        "watch_files": [
            "data/portfolio_recommendation/portfolio_recommendation.json",
            "data/portfolio_recommendation/target_positions.csv",
        ],
        "risk_hint": "如果组合文件未刷新，先回看 V5 最新 cycle_summary.json。",
    },
    "execution_bridge": {
        "watch_files": [
            "data/live_execution_bridge/execution_report_*.json",
            "data/live_execution_bridge/latest_account_state.json",
            "data/live_execution_bridge/equity_curve.csv",
        ],
        "risk_hint": "执行桥阶段优先关注价格缺失和账户状态同步失败。",
    },
}


def _default_runtime_note(stage_name: str, stage_label: str, status: str) -> Dict[str, Any]:
    mapping = _RUNTIME_STAGE_WATCH.get(stage_name, {})
    return {
        "stage_name": stage_name,
        "stage_label": stage_label,
        "status": status,
        "operator_note": f"当前进入 {stage_label}，这是本轮长阶段之一，可按建议文件观察进度。",
        "watch_files": list(mapping.get("watch_files", [])),
        "risk_hint": _safe_text(mapping.get("risk_hint")) or "保持关注日志和状态文件是否持续刷新。",
    }


def emit_runtime_stage_note(
    config: Dict[str, Any],
    stage_name: str,
    stage_label: str,
    status: str = "running",
    summary: str = "",
) -> Dict[str, Any]:
    if stage_name not in set(_runtime_explainer_stages(config)):
        return {}
    root = ensure_dir(_research_root(config) / "supervisor")
    out_path = root / "runtime_stage_notes.json"
    default_note = _default_runtime_note(stage_name=stage_name, stage_label=stage_label, status=status)
    mapping = _RUNTIME_STAGE_WATCH.get(stage_name, {})
    system_prompt = (
        "你是量化系统运行过程中的操作提示器。"
        "只输出一个 JSON 对象，不要输出解释，不要输出 markdown。"
    )
    user_prompt = json.dumps(
        {
            "task": "给操作员一条当前阶段说明",
            "schema": {
                "operator_note": "一句话说明当前在做什么和下一步看什么",
                "watch_files": ["最多 3 个要观察的文件"],
                "risk_hint": "一句话提醒",
            },
            "stage_name": stage_name,
            "stage_label": stage_label,
            "status": status,
            "summary": summary,
            "watch_files": list(mapping.get("watch_files", [])),
            "risk_hint": _safe_text(mapping.get("risk_hint")),
        },
        ensure_ascii=False,
    )
    result = _call_role_json(
        config=config,
        role_name="runtime_explainer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        fallback_model="qwen2.5:7b",
        fallback_timeout_seconds=45,
    )
    payload = dict(result.get("data", {}) or {})
    note = {
        "timestamp": _now_text(),
        "stage_name": stage_name,
        "stage_label": stage_label,
        "status": status,
        "operator_note": _safe_text(payload.get("operator_note"))[:220] or default_note["operator_note"],
        "watch_files": _string_list(payload.get("watch_files") or default_note["watch_files"], max_items=3, max_chars=160),
        "risk_hint": _safe_text(payload.get("risk_hint"))[:180] or default_note["risk_hint"],
        "llm_ok": bool(result.get("ok", False)),
        "llm_model": _safe_text(result.get("model")),
    }
    _append_json_array(out_path, note, max_items=80)
    watch_hint = ", ".join(note["watch_files"][:2])
    log_line(config, f"Supervisor Note: {note['operator_note']} watch={watch_hint}")
    return note


def _fallback_v5_review(payload: Dict[str, Any]) -> Dict[str, Any]:
    top_results = list(payload.get("top_results", []) or [])
    issues = _string_list(payload.get("diagnosis_issues"), max_items=6, max_chars=80)
    headline = "本轮 V5 研究已完成，但仍需结合 cycle_summary.json 做人工复核。"
    if top_results:
        top = top_results[0]
        headline = (
            f"本轮 V5 完成，当前最佳候选 {top.get('strategy_name', '')} "
            f"route={top.get('research_route', '')} total_score={top.get('total_score', 0.0)}。"
        )
    strengths = ["已有可落地的最佳候选输出。"] if top_results else ["V5 已顺利完成一轮候选生成。"]
    weaknesses = [f"诊断问题: {', '.join(issues)}"] if issues else ["尚未形成足够明确的下轮偏置。"]
    next_focus = ["优先检查最佳候选的特征、模型与训练逻辑组合是否稳定。", "对照 cycle_summary.json 看是否存在单一路线过拟合。"]
    return {
        "headline": headline[:220],
        "strengths": strengths,
        "weaknesses": weaknesses,
        "next_focus": next_focus,
        "operator_note": "先看 latest_v5_cycle_review.json，再对照 controller_state.json 和最新 cycle_summary.json。",
    }


def build_v5_cycle_review(config: Dict[str, Any]) -> Dict[str, Any]:
    hub_root = Path(str(config.get("research_brain", {}).get("hub_output_root", "") or "").strip())
    if not hub_root.exists():
        return {"ok": False, "error": "missing_v5_hub_output_root"}
    controller_state_path = hub_root / "controller_state.json"
    controller_state = _load_json(controller_state_path)
    cycle_id = _safe_text(controller_state.get("last_cycle_id"))
    cycle_summary_path = hub_root / "cycles" / cycle_id / "cycle_summary.json" if cycle_id else Path()
    cycle_summary = _load_json(cycle_summary_path) if cycle_id else {}
    results = list(cycle_summary.get("results", []) or [])
    if not cycle_id or not cycle_summary:
        return {"ok": False, "error": "missing_v5_cycle_summary"}

    def _compact_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "strategy_name": _safe_text(row.get("strategy_name"))[:80],
            "research_route": _safe_text(row.get("research_route"))[:24],
            "model_family": _safe_text(row.get("model_family"))[:24],
            "feature_profile": _safe_text(row.get("feature_profile"))[:32],
            "training_logic": _safe_text(row.get("training_logic"))[:24],
            "total_score": float(row.get("total_score", 0.0) or 0.0),
            "sharpe": float(row.get("sharpe", 0.0) or 0.0),
            "valid_ic": float(row.get("valid_ic", 0.0) or 0.0),
            "test_ic": float(row.get("test_ic", 0.0) or 0.0),
            "status": _safe_text(row.get("status")),
        }

    sorted_results = sorted(results, key=lambda row: float(row.get("total_score", 0.0) or 0.0), reverse=True)
    compact_payload = {
        "cycle_id": cycle_id,
        "n_candidates": int(cycle_summary.get("n_candidates", len(results)) or len(results)),
        "budget": dict(cycle_summary.get("budget", {}) or {}),
        "diagnosis_issues": list(cycle_summary.get("diagnosis", {}).get("issues", []) or []),
        "gate": dict(cycle_summary.get("gate", {}) or {}),
        "top_results": [_compact_row(row) for row in sorted_results[:4]],
        "bottom_results": [_compact_row(row) for row in list(reversed(sorted_results[-2:]))],
    }
    system_prompt = (
        "你是量化研究主管。"
        "只输出一个 JSON 对象，不要输出解释，不要输出 markdown。"
    )
    user_prompt = json.dumps(
        {
            "task": "总结本轮 V5 研究结果，给操作员一份简洁复盘",
            "schema": {
                "headline": "一句话结论",
                "strengths": ["最多 3 条"],
                "weaknesses": ["最多 3 条"],
                "next_focus": ["最多 3 条"],
                "operator_note": "一句操作提醒",
            },
            "cycle_summary": compact_payload,
        },
        ensure_ascii=False,
    )
    result = _call_role_json(
        config=config,
        role_name="v5_review",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        fallback_model="deepseek-r1:14b",
        fallback_timeout_seconds=180,
    )
    payload = dict(result.get("data", {}) or {}) if bool(result.get("ok", False)) else {}
    if not payload:
        payload = _fallback_v5_review(compact_payload)
    fallback = _fallback_v5_review(compact_payload)
    review = {
        "generated_at": _now_text(),
        "cycle_id": cycle_id,
        "headline": _safe_text(payload.get("headline"))[:220] or fallback["headline"],
        "strengths": _string_list(payload.get("strengths"), max_items=3, max_chars=160) or fallback["strengths"],
        "weaknesses": _string_list(payload.get("weaknesses"), max_items=3, max_chars=160) or fallback["weaknesses"],
        "next_focus": _string_list(payload.get("next_focus"), max_items=3, max_chars=160) or fallback["next_focus"],
        "operator_note": _safe_text(payload.get("operator_note"))[:220] or fallback["operator_note"],
        "llm_ok": bool(result.get("ok", False)),
        "llm_model": _safe_text(result.get("model")),
        "source_paths": {
            "controller_state": str(controller_state_path),
            "cycle_summary": str(cycle_summary_path),
        },
        "compact_cycle_summary": compact_payload,
    }
    review_root = ensure_dir(hub_root / "reviews")
    latest_path = review_root / "latest_v5_cycle_review.json"
    timestamped_path = review_root / f"v5_cycle_review_{cycle_id}.json"
    _write_json(latest_path, review)
    _write_json(timestamped_path, review)
    log_line(config, f"Supervisor: V5 本地复盘已生成 cycle_id={cycle_id} model={review['llm_model'] or 'fallback'}")
    return {"ok": True, "review": review, "path": str(latest_path)}
