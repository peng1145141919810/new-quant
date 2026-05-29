from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir
from .llm_trace import write_llm_trace
from .llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient


def _top_rows(candidate_rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in candidate_rows[: max(int(limit or 0), 1)]:
        rows.append(
            {
                "symbol": str(item.get("ts_code") or item.get("symbol") or "").strip().upper(),
                "industry": str(item.get("industry") or "").strip(),
                "selection_score": float(item.get("selection_score") or 0.0),
                "integrated_thesis_score": float(item.get("integrated_thesis_score") or 0.0),
                "integrated_thesis_state": str(item.get("integrated_thesis_state") or "").strip(),
                "primary_event_type": str(item.get("primary_event_type") or "").strip(),
                "primary_mechanism_group": str(item.get("primary_mechanism_group") or item.get("mechanism_primary") or "").strip(),
                "router_final_score": float(item.get("router_final_score") or 0.0),
                "event_fact_backed": bool(item.get("event_fact_backed", False)),
                "candidate_tier": str(item.get("candidate_tier") or "").strip(),
                "price": float(item.get("price") or 0.0),
            }
        )
    return rows


def _build_prompt(
    *,
    market_state: Dict[str, Any],
    account_state: Dict[str, Any],
    thesis_summary: Dict[str, Any],
    candidate_rows: List[Dict[str, Any]],
) -> Dict[str, str]:
    system_prompt = (
        "你是A股候选池顾问。你的职责不是直接下单，而是帮助系统在候选池过窄、过硬回退时做保守扩容和排序辅助。"
        "你必须只输出JSON。"
        "你只能通过这些字段给建议："
        "pool_quality、review_summary、favored_symbols、blocked_symbols、favored_mechanisms、favored_event_types、"
        "target_candidate_count、breadth_bias、risk_flags。"
        "规则："
        "1. favored_symbols 最多8个，只能来自输入候选。"
        "2. blocked_symbols 最多8个，只能来自输入候选。"
        "3. target_candidate_count 必须在 6 到 40 之间。"
        "4. breadth_bias 只能是 narrow、balanced、broad。"
        "5. 当 integrated_thesis 接受数很低时，允许建议 broad，但不能编造新股票。"
    )
    payload = {
        "task": "评估当前候选池是否过窄，并给出保守扩容排序建议",
        "required_json_schema": {
            "pool_quality": "strong|mixed|weak",
            "review_summary": "string",
            "favored_symbols": ["string"],
            "blocked_symbols": ["string"],
            "favored_mechanisms": ["string"],
            "favored_event_types": ["string"],
            "target_candidate_count": "int",
            "breadth_bias": "narrow|balanced|broad",
            "risk_flags": ["string"],
            "decision_basis": ["string"],
            "uncertainty_flags": ["string"],
            "overfit_guard": "string",
        },
        "market_state": {
            "market_regime": str(market_state.get("market_regime", "") or ""),
            "style_bias": str(market_state.get("style_bias", "") or ""),
            "mechanism_bias": str(market_state.get("mechanism_bias", "") or ""),
            "risk_budget_multiplier": float(market_state.get("risk_budget_multiplier", 1.0) or 1.0),
            "new_position_policy": str(market_state.get("new_position_policy", "") or ""),
        },
        "account_state": {
            "account_id": str(account_state.get("account_id", "") or ""),
            "nav": float(account_state.get("nav", 0.0) or 0.0),
            "cash": float(account_state.get("cash", 0.0) or 0.0),
            "positions_count": int(account_state.get("positions_count", 0) or 0),
        },
        "integrated_thesis_summary": {
            "accepted_thesis_count": int(thesis_summary.get("n_accepted", 0) or 0),
            "symbol_count": int(thesis_summary.get("n_symbols", 0) or 0),
            "fact_backed_candidates": int(thesis_summary.get("n_fact_backed_candidates", 0) or 0),
            "top_candidate_count": int(thesis_summary.get("top_candidate_count", 0) or 0),
        },
        "candidate_rows": candidate_rows,
    }
    return {"system_prompt": system_prompt, "user_prompt": json.dumps(payload, ensure_ascii=False)}


def _default_review(reason: str, thesis_summary: Dict[str, Any], candidate_count: int) -> Dict[str, Any]:
    accepted = int(thesis_summary.get("n_accepted", 0) or 0)
    fact_backed = int(thesis_summary.get("n_fact_backed_candidates", 0) or 0)
    if accepted >= 8 and fact_backed >= 6:
        pool_quality = "strong"
    elif accepted >= 2 or fact_backed >= 2:
        pool_quality = "mixed"
    else:
        pool_quality = "weak"
    return {
        "ok": False,
        "pool_quality": pool_quality,
        "review_summary": reason,
        "favored_symbols": [],
        "blocked_symbols": [],
        "favored_mechanisms": [],
        "favored_event_types": [],
        "target_candidate_count": max(6, min(max(int(candidate_count or 0), 6), 24)),
        "breadth_bias": "broad" if pool_quality == "weak" else "balanced",
        "risk_flags": ["candidate_pool_llm_unavailable"],
        "decision_basis": [],
        "uncertainty_flags": ["llm_unavailable"],
        "overfit_guard": "fallback_to_quant_scores_only",
        "provider": "",
        "model": "",
        "error": reason,
    }


def _normalize_review(raw: Dict[str, Any], allowed_symbols: List[str], candidate_count: int) -> Dict[str, Any]:
    allowed = {str(item).strip().upper() for item in allowed_symbols if str(item).strip()}
    favored_symbols: List[str] = []
    for item in list(raw.get("favored_symbols", []) or []):
        symbol = str(item).strip().upper()
        if symbol and symbol in allowed and symbol not in favored_symbols:
            favored_symbols.append(symbol)
    blocked_symbols: List[str] = []
    for item in list(raw.get("blocked_symbols", []) or []):
        symbol = str(item).strip().upper()
        if symbol and symbol in allowed and symbol not in blocked_symbols:
            blocked_symbols.append(symbol)
    favored_symbols = [item for item in favored_symbols if item not in blocked_symbols][:8]
    blocked_symbols = blocked_symbols[:8]
    favored_mechanisms = []
    for item in list(raw.get("favored_mechanisms", []) or []):
        text = str(item).strip()
        if text and text not in favored_mechanisms:
            favored_mechanisms.append(text)
    favored_event_types = []
    for item in list(raw.get("favored_event_types", []) or []):
        text = str(item).strip()
        if text and text not in favored_event_types:
            favored_event_types.append(text)
    pool_quality = str(raw.get("pool_quality", "mixed") or "mixed").strip().lower()
    if pool_quality not in {"strong", "mixed", "weak"}:
        pool_quality = "mixed"
    breadth_bias = str(raw.get("breadth_bias", "balanced") or "balanced").strip().lower()
    if breadth_bias not in {"narrow", "balanced", "broad"}:
        breadth_bias = "balanced"
    try:
        target_candidate_count = int(raw.get("target_candidate_count", candidate_count) or candidate_count)
    except Exception:
        target_candidate_count = int(candidate_count or 0)
    target_candidate_count = max(6, min(target_candidate_count, 40))
    return {
        "ok": True,
        "pool_quality": pool_quality,
        "review_summary": str(raw.get("review_summary", "") or "").strip(),
        "favored_symbols": favored_symbols,
        "blocked_symbols": blocked_symbols,
        "favored_mechanisms": favored_mechanisms[:4],
        "favored_event_types": favored_event_types[:4],
        "target_candidate_count": target_candidate_count,
        "breadth_bias": breadth_bias,
        "risk_flags": [str(item).strip() for item in list(raw.get("risk_flags", []) or []) if str(item).strip()][:6],
        "decision_basis": [str(item).strip() for item in list(raw.get("decision_basis", []) or []) if str(item).strip()][:4],
        "uncertainty_flags": [str(item).strip() for item in list(raw.get("uncertainty_flags", []) or []) if str(item).strip()][:6],
        "overfit_guard": str(raw.get("overfit_guard", "") or "").strip()[:240],
    }


def review_candidate_pool(
    *,
    config: Dict[str, Any],
    market_state: Dict[str, Any],
    account_state: Dict[str, Any],
    thesis_summary: Dict[str, Any],
    candidate_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    cfg = dict(config.get("portfolio_candidate_llm_review", {}) or {})
    if not bool(cfg.get("enabled", False)):
        return {"enabled": False, "applied": False}
    sample_rows = _top_rows(candidate_rows, limit=int(cfg.get("max_input_rows", 18) or 18))
    allowed_symbols = [str(item.get("symbol") or item.get("ts_code") or "").strip().upper() for item in sample_rows]
    prompts = _build_prompt(
        market_state=market_state,
        account_state=account_state,
        thesis_summary=thesis_summary,
        candidate_rows=sample_rows,
    )
    provider = str(cfg.get("provider", "deepseek_worker") or "deepseek_worker").strip()
    timeout_seconds = int(cfg.get("timeout_seconds", 45) or 45)
    providers_cfg = dict(config.get("providers", {}) or {})
    if provider == "local_ollama":
        client = LocalOllamaChatClient(provider_name="candidate_pool_ollama", cfg=dict(config.get("local_ollama", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    elif provider == "openai_research":
        client = OpenAIResponsesClient(provider_name="candidate_pool_openai", cfg=dict(providers_cfg.get("openai_research", {}) or {}))
        result = client.create_json_response_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    else:
        client = DeepSeekChatClient(provider_name="candidate_pool_deepseek", cfg=dict(providers_cfg.get("deepseek_worker", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    if not bool(result.get("ok", False)):
        normalized = _default_review(str(result.get("error_message", "") or "candidate_pool_llm_failed"), thesis_summary, len(candidate_rows))
    else:
        normalized = _normalize_review(dict(result.get("data", {}) or {}), allowed_symbols, len(candidate_rows))
        normalized["provider"] = str(result.get("provider", "") or "")
        normalized["model"] = str(result.get("model", "") or "")
        normalized["elapsed_seconds"] = float(result.get("elapsed_seconds", 0.0) or 0.0)
        normalized["error"] = ""
    artifact_root = ensure_dir(Path(str(cfg.get("artifact_root", "") or "")).resolve())
    latest_root = ensure_dir(artifact_root / "latest")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": int(len(candidate_rows)),
        "input_sample": sample_rows,
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
    artifact_path = artifact_root / f"candidate_pool_llm_review_{timestamp}.json"
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path = latest_root / "latest_candidate_pool_llm_review.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["trace"] = write_llm_trace(
        config=config,
        stage="candidate_pool_review",
        provider=provider,
        system_prompt=prompts["system_prompt"],
        user_prompt=prompts["user_prompt"],
        result=result,
        normalized_review=normalized,
        input_payload={"candidate_rows": sample_rows},
        meta={"candidate_count": int(len(candidate_rows))},
    )
    payload["artifact_path"] = str(artifact_path)
    payload["latest_artifact_path"] = str(latest_path)
    payload["enabled"] = True
    payload["applied"] = bool(normalized.get("ok", False))
    return payload
