# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir
from .llm_trace import write_llm_trace
from .llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient


def _read_target_rows(path: Path, limit: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "symbol": str(row.get("symbol") or row.get("ts_code") or "").strip(),
                    "target_weight": float(row.get("portfolio_weight") or row.get("target_weight") or 0.0),
                    "score": float(row.get("selection_score") or row.get("score") or 0.0),
                    "industry": str(row.get("industry") or row.get("industry_name") or "").strip(),
                    "mechanism_primary": str(row.get("mechanism_primary") or "").strip(),
                    "primary_event_type": str(row.get("primary_event_type") or "").strip(),
                    "desired_action": str(row.get("desired_action") or row.get("recommended_action") or "").strip(),
                    "thesis_gate_stage": str(row.get("thesis_gate_stage") or "").strip(),
                    "thesis_reject_reason": str(row.get("thesis_reject_reason") or "").strip(),
                }
            )
            if len(rows) >= max(limit, 1):
                break
    return rows


def _build_prompt(
    release_doc: Dict[str, Any],
    market_state: Dict[str, Any],
    account_state: Dict[str, Any],
    safety: Dict[str, Any],
    targets: List[Dict[str, Any]],
    thesis_summary: Dict[str, Any],
) -> Dict[str, str]:
    system_prompt = (
        "你是A股盘前执行审查助手。你的职责不是选股，而是在正式下单前对当天计划做最后一层审查。"
        "你必须输出严格 JSON，只允许保守影响执行，不允许编造市场数据。"
        "你的输出只能通过这些字段影响执行："
        "turnover_multiplier、blocked_symbols、favored_symbols、reduce_only、review_summary、risk_flags、candidate_pool_assessment。"
        "规则："
        "1. blocked_symbols 最多 3 个，只能填今天计划里的标的。"
        "2. favored_symbols 最多 3 个，只能填今天计划里的标的。"
        "3. turnover_multiplier 必须在 0.6 到 1.15 之间。"
        "4. 如果候选池质量明显不足，可以建议 reduce_only=true。"
        "5. 不能输出 markdown，不能输出解释文本，不能输出 schema 以外字段。"
    )
    payload = {
        "task": "审查今天的下单计划并返回 JSON",
        "required_json_schema": {
            "review_summary": "string",
            "risk_level": "low|medium|high",
            "turnover_multiplier": "float",
            "blocked_symbols": ["string"],
            "favored_symbols": ["string"],
            "reduce_only": "bool",
            "risk_flags": ["string"],
            "decision_basis": ["string"],
            "uncertainty_flags": ["string"],
            "overfit_guard": "string",
            "candidate_pool_assessment": {
                "pool_quality": "strong|mixed|weak",
                "reason": "string",
                "accepted_thesis_count": "int",
                "fallback_source": "string",
            },
        },
        "release": {
            "release_id": str(release_doc.get("release_id", "") or ""),
            "trade_date": str(release_doc.get("trade_date", "") or ""),
            "profile": str(release_doc.get("profile", "") or ""),
        },
        "market_state": {
            "market_regime": str(market_state.get("market_regime", "") or ""),
            "style_bias": str(market_state.get("style_bias", "") or ""),
            "mechanism_bias": str(market_state.get("mechanism_bias", "") or ""),
            "turnover_multiplier": float(market_state.get("turnover_multiplier", 1.0) or 1.0),
            "new_position_policy": str(market_state.get("new_position_policy", "") or ""),
        },
        "account_state": {
            "account_id": str(account_state.get("account_id", "") or ""),
            "cash": float(account_state.get("cash", 0.0) or 0.0),
            "nav": float(account_state.get("nav", 0.0) or 0.0),
            "positions_count": int(account_state.get("positions_count", 0) or 0),
        },
        "safety": {
            "system_mode": str(safety.get("system_mode", "") or ""),
            "market_safety_regime": str(safety.get("market_safety_regime", "") or ""),
            "effective_turnover_multiplier": float(safety.get("effective_turnover_multiplier", 1.0) or 1.0),
            "effective_reduce_only": bool(safety.get("effective_reduce_only", False)),
        },
        "integrated_thesis_summary": thesis_summary,
        "targets": targets,
    }
    return {"system_prompt": system_prompt, "user_prompt": json.dumps(payload, ensure_ascii=False)}


def _normalized_default(reason: str, thesis_summary: Dict[str, Any]) -> Dict[str, Any]:
    fallback_source = str(thesis_summary.get("fallback_source", "") or "")
    accepted = int(thesis_summary.get("accepted_thesis_count", 0) or 0)
    pool_quality = "strong" if accepted >= 4 else ("mixed" if accepted >= 1 else "weak")
    return {
        "ok": False,
        "review_summary": reason,
        "risk_level": "medium" if pool_quality == "mixed" else "high" if pool_quality == "weak" else "low",
        "turnover_multiplier": 1.0,
        "blocked_symbols": [],
        "favored_symbols": [],
        "reduce_only": False,
        "risk_flags": ["llm_review_unavailable"],
        "decision_basis": [],
        "uncertainty_flags": ["llm_unavailable"],
        "overfit_guard": "fallback_to_market_state_and_safety_only",
        "candidate_pool_assessment": {
            "pool_quality": pool_quality,
            "reason": reason,
            "accepted_thesis_count": accepted,
            "fallback_source": fallback_source,
        },
        "provider": "",
        "model": "",
        "error": reason,
    }


def _load_latest_recommendation_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    out_root = Path(str(config.get("paths", {}).get("portfolio_output_root", "") or "")).resolve()
    path = out_root / "portfolio_recommendation.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalize_payload(raw: Dict[str, Any], cfg: Dict[str, Any], plan_symbols: List[str]) -> Dict[str, Any]:
    allowed_symbols = {str(item).strip().upper() for item in plan_symbols if str(item).strip()}
    turnover_floor = float(cfg.get("turnover_multiplier_floor", 0.6) or 0.6)
    turnover_cap = float(cfg.get("turnover_multiplier_cap", 1.15) or 1.15)
    blocked = []
    for item in list(raw.get("blocked_symbols", []) or []):
        symbol = str(item).strip().upper()
        if symbol and symbol in allowed_symbols and symbol not in blocked:
            blocked.append(symbol)
    blocked = blocked[: max(int(cfg.get("max_blocked_symbols", 3) or 3), 0)]
    favored = []
    for item in list(raw.get("favored_symbols", []) or []):
        symbol = str(item).strip().upper()
        if symbol and symbol in allowed_symbols and symbol not in favored and symbol not in blocked:
            favored.append(symbol)
    favored = favored[:3]
    try:
        turnover_multiplier = float(raw.get("turnover_multiplier", 1.0) or 1.0)
    except Exception:
        turnover_multiplier = 1.0
    turnover_multiplier = max(turnover_floor, min(turnover_cap, turnover_multiplier))
    reduce_only = bool(raw.get("reduce_only", False) and cfg.get("allow_reduce_only", True))
    risk_level = str(raw.get("risk_level", "medium") or "medium").strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"
    candidate_pool_assessment = dict(raw.get("candidate_pool_assessment", {}) or {})
    pool_quality = str(candidate_pool_assessment.get("pool_quality", "mixed") or "mixed").strip().lower()
    if pool_quality not in {"strong", "mixed", "weak"}:
        pool_quality = "mixed"
    return {
        "ok": True,
        "review_summary": str(raw.get("review_summary", "") or "").strip(),
        "risk_level": risk_level,
        "turnover_multiplier": round(turnover_multiplier, 4),
        "blocked_symbols": blocked,
        "favored_symbols": favored,
        "reduce_only": reduce_only,
        "risk_flags": [str(item).strip() for item in list(raw.get("risk_flags", []) or []) if str(item).strip()][:6],
        "decision_basis": [str(item).strip() for item in list(raw.get("decision_basis", []) or []) if str(item).strip()][:4],
        "uncertainty_flags": [str(item).strip() for item in list(raw.get("uncertainty_flags", []) or []) if str(item).strip()][:6],
        "overfit_guard": str(raw.get("overfit_guard", "") or "").strip()[:240],
        "candidate_pool_assessment": {
            "pool_quality": pool_quality,
            "reason": str(candidate_pool_assessment.get("reason", "") or "").strip(),
            "accepted_thesis_count": int(candidate_pool_assessment.get("accepted_thesis_count", 0) or 0),
            "fallback_source": str(candidate_pool_assessment.get("fallback_source", "") or "").strip(),
        },
    }


def review_execution_plan(
    *,
    config: Dict[str, Any],
    release_doc: Dict[str, Any],
    market_state: Dict[str, Any],
    account_state: Dict[str, Any],
    safety: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = dict(config.get("execution_llm_review", {}) or {})
    if not bool(cfg.get("enabled", False)):
        return {"enabled": False, "applied": False}
    target_positions_path_text = str(
        release_doc.get("artifacts", {}).get("target_positions_path")
        or release_doc.get("target_positions_path")
        or ""
    ).strip()
    target_positions_path = Path(target_positions_path_text).resolve() if target_positions_path_text else Path()
    max_target_items = int(cfg.get("max_target_items", 8) or 8)
    targets = _read_target_rows(target_positions_path, limit=max_target_items)
    plan_symbols = [str(item.get("symbol", "") or "").strip().upper() for item in targets if str(item.get("symbol", "") or "").strip()]
    latest_recommendation = _load_latest_recommendation_summary(config=config)
    thesis_state = dict(release_doc.get("integrated_thesis_state", {}) or latest_recommendation.get("integrated_thesis_state", {}) or {})
    thesis_summary = dict((thesis_state.get("summary") or {}))
    thesis_brief = {
        "accepted_thesis_count": int(thesis_summary.get("n_accepted", 0) or 0),
        "symbol_count": int(thesis_summary.get("n_symbols", 0) or 0),
        "fact_backed_candidates": int(thesis_summary.get("n_fact_backed_candidates", 0) or 0),
        "fallback_source": str(release_doc.get("candidate_source", "") or latest_recommendation.get("candidate_source", "") or ""),
    }
    prompts = _build_prompt(
        release_doc=release_doc,
        market_state=market_state,
        account_state=account_state,
        safety=safety,
        targets=targets,
        thesis_summary=thesis_brief,
    )
    provider = str(cfg.get("provider", "deepseek_worker") or "deepseek_worker").strip()
    timeout_seconds = int(cfg.get("timeout_seconds", 45) or 45)
    provider_cfg = dict(config.get("providers", {}) or {})
    result: Dict[str, Any]
    if provider == "local_ollama":
        client = LocalOllamaChatClient(provider_name="execution_llm_review_ollama", cfg=dict(config.get("local_ollama", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    elif provider == "openai_research":
        client = OpenAIResponsesClient(provider_name="execution_llm_review_openai", cfg=dict(provider_cfg.get("openai_research", {}) or {}))
        result = client.create_json_response_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    else:
        client = DeepSeekChatClient(provider_name="execution_llm_review_deepseek", cfg=dict(provider_cfg.get("deepseek_worker", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    raw_data = dict(result.get("data", {}) or {})
    if not bool(result.get("ok", False)):
        normalized = _normalized_default(str(result.get("error_message", "") or "llm_review_failed"), thesis_summary=thesis_brief)
    else:
        normalized = _normalize_payload(raw_data, cfg=cfg, plan_symbols=plan_symbols)
        normalized["provider"] = str(result.get("provider", "") or "")
        normalized["model"] = str(result.get("model", "") or "")
        normalized["elapsed_seconds"] = float(result.get("elapsed_seconds", 0.0) or 0.0)
        normalized["error"] = ""
    artifact_root = ensure_dir(Path(str(cfg.get("artifact_root", "") or "")).resolve())
    latest_root = ensure_dir(artifact_root / "latest")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "release_id": str(release_doc.get("release_id", "") or ""),
        "trade_date": str(release_doc.get("trade_date", "") or ""),
        "provider": str(normalized.get("provider", "") or ""),
        "model": str(normalized.get("model", "") or ""),
        "target_positions_path": str(target_positions_path),
        "input_summary": {
            "n_targets": len(targets),
            "account_id": str(account_state.get("account_id", "") or ""),
            "nav": float(account_state.get("nav", 0.0) or 0.0),
            "cash": float(account_state.get("cash", 0.0) or 0.0),
            "accepted_thesis_count": int(thesis_brief.get("accepted_thesis_count", 0) or 0),
            "fallback_source": str(thesis_brief.get("fallback_source", "") or ""),
        },
        "review": normalized,
        "raw_model_result": {
            "ok": bool(result.get("ok", False)),
            "provider": str(result.get("provider", "") or ""),
            "model": str(result.get("model", "") or ""),
            "error_type": str(result.get("error_type", "") or ""),
            "error_message": str(result.get("error_message", "") or ""),
            "elapsed_seconds": float(result.get("elapsed_seconds", 0.0) or 0.0),
        },
    }
    artifact_path = artifact_root / f"execution_llm_review_{timestamp}.json"
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path = latest_root / "latest_execution_llm_review.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["trace"] = write_llm_trace(
        config=config,
        stage="execution_review",
        provider=provider,
        system_prompt=prompts["system_prompt"],
        user_prompt=prompts["user_prompt"],
        result=result,
        normalized_review=normalized,
        input_payload={"targets": targets, "thesis_summary": thesis_brief},
        meta={"release_id": str(release_doc.get("release_id", "") or "")},
    )
    payload["artifact_path"] = str(artifact_path)
    payload["latest_artifact_path"] = str(latest_path)
    payload["enabled"] = True
    payload["applied"] = bool(normalized.get("ok", False))
    return payload
