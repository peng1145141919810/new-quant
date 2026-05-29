from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .config_utils import ensure_dir
from .llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient
from .llm_trace import write_llm_trace


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _candidate_rows(df: pd.DataFrame, limit: int = 10) -> List[Dict[str, Any]]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    rows: List[Dict[str, Any]] = []
    for _, row in df.head(max(int(limit), 1)).iterrows():
        rows.append(
            {
                "symbol": _text(row.get("ts_code") or row.get("symbol") or row.get("code")).upper(),
                "alpha_family": _text(row.get("activation_alpha_family") or row.get("alpha_family")),
                "priority": round(_safe_float(row.get("alpha_activation_priority") or row.get("outer_intelligence_priority")), 6),
                "portfolio_weight": round(_safe_float(row.get("portfolio_weight")), 6),
                "industry": _text(row.get("industry")),
                "reason_code": _text(row.get("reason_code")),
            }
        )
    return rows


def _default_operating_brain(reason: str) -> Dict[str, Any]:
    return {
        "enabled": True,
        "applied": False,
        "provider": "",
        "model": "",
        "research_brain": {
            "focus_families": [],
            "data_refresh_priorities": [],
            "decision_basis": [],
        },
        "dispatch_brain": {
            "preferred_posture": "balanced",
            "cash_posture": "hold_buffer",
            "tactical_bias": "reduce_risk_first",
            "decision_basis": [],
        },
        "operations_brain": {
            "watch_items": ["llm_unavailable"],
            "incident_actions": [],
            "decision_basis": [],
        },
        "uncertainty_flags": ["llm_unavailable"],
        "overfit_guard": "fallback_to_quant_allocator_and_safety_layers",
        "error": reason,
    }


def _client_for(config: Dict[str, Any], provider: str):
    providers_cfg = dict(config.get("providers", {}) or {})
    if provider == "local_ollama":
        return LocalOllamaChatClient(provider_name="llm_operating_brain_ollama", cfg=dict(config.get("local_ollama", {}) or {}))
    if provider == "openai_research":
        return OpenAIResponsesClient(provider_name="llm_operating_brain_openai", cfg=dict(providers_cfg.get("openai_research", {}) or {}))
    return DeepSeekChatClient(provider_name="llm_operating_brain_deepseek", cfg=dict(providers_cfg.get("deepseek_worker", {}) or {}))


def _chat_json(client: Any, provider: str, system_prompt: str, user_prompt: str, timeout_seconds: int) -> Dict[str, Any]:
    if provider == "openai_research":
        return client.create_json_response_detailed(system_prompt, user_prompt, timeout_seconds=timeout_seconds)
    return client.chat_json_detailed(system_prompt, user_prompt, timeout_seconds=timeout_seconds)


def build_operating_brain(
    *,
    config: Dict[str, Any],
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    candidate_df: pd.DataFrame,
    alpha_lifecycle: Dict[str, Any],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = dict(config.get("llm_operating_brain", {}) or {})
    if not bool(cfg.get("enabled", True)):
        return {"enabled": False, "applied": False}
    provider = _text(cfg.get("provider") or "deepseek_worker") or "deepseek_worker"
    timeout_seconds = int(cfg.get("timeout_seconds", 45) or 45)
    candidate_rows = _candidate_rows(candidate_df, limit=int(cfg.get("max_candidate_items", 10) or 10))
    alpha_items = list(dict(alpha_lifecycle or {}).get("items", []) or [])[:8]
    payload = {
      "task": "Act as the operating brain of an A-share multi-alpha system. Return strict JSON only.",
      "required_json_schema": {
        "research_brain": {
          "focus_families": ["string"],
          "data_refresh_priorities": ["string"],
          "decision_basis": ["string"]
        },
        "dispatch_brain": {
          "preferred_posture": "aggressive|balanced|defensive",
          "cash_posture": "deploy|hold_buffer|raise_cash",
          "tactical_bias": "reduce_risk_first|rebuild_core|expand_winners",
          "decision_basis": ["string"]
        },
        "operations_brain": {
          "watch_items": ["string"],
          "incident_actions": ["string"],
          "decision_basis": ["string"]
        },
        "uncertainty_flags": ["string"],
        "overfit_guard": "string"
      },
      "rules": [
        "Do not invent unavailable data sources.",
        "Respect diversification-first and avoid heavy concentration.",
        "Treat intraday T as a portfolio service layer, not a standalone alpha.",
        "Prefer alpha families with promote/live status over demote status."
      ],
      "market_state": {
        "market_regime": _text(market_state.get("market_regime")),
        "style_bias": _text(market_state.get("style_bias")),
        "mechanism_bias": _text(market_state.get("mechanism_bias")),
        "risk_budget_multiplier": _safe_float(market_state.get("risk_budget_multiplier"), 1.0),
      },
      "account_state": {
        "nav": _safe_float(account_ctx.get("nav")),
        "cash": _safe_float(account_ctx.get("cash")),
        "positions_count": int(account_ctx.get("positions_count") or 0),
      },
      "alpha_lifecycle": alpha_items,
      "candidate_rows": candidate_rows,
      "summary": {
        "account_allocator_profile": dict(summary.get("account_allocator_profile", {}) or {}),
        "diversification_objective": dict(summary.get("diversification_objective", {}) or {}),
        "candidate_pool_stats": dict(summary.get("candidate_pool_stats", {}) or {}),
      },
    }
    system_prompt = (
        "You are the research-dispatch-operations brain for an aggressive but disciplined A-share multi-alpha system. "
        "You leave a forensic trail, avoid black-box silent decisions, and optimize for stable deployment instead of overfit heroics."
    )
    user_prompt = json.dumps(payload, ensure_ascii=False)
    result: Dict[str, Any] = {}
    normalized = _default_operating_brain("llm_operating_brain_unavailable")
    try:
        client = _client_for(config, provider)
        result = _chat_json(client, provider, system_prompt, user_prompt, timeout_seconds)
        if bool(result.get("ok", False)):
            raw = dict(result.get("data", {}) or {})
            normalized = {
                "enabled": True,
                "applied": True,
                "provider": _text(result.get("provider") or provider),
                "model": _text(result.get("model")),
                "research_brain": {
                    "focus_families": [_text(item) for item in list((raw.get("research_brain", {}) or {}).get("focus_families", []) or []) if _text(item)][:6],
                    "data_refresh_priorities": [_text(item) for item in list((raw.get("research_brain", {}) or {}).get("data_refresh_priorities", []) or []) if _text(item)][:6],
                    "decision_basis": [_text(item) for item in list((raw.get("research_brain", {}) or {}).get("decision_basis", []) or []) if _text(item)][:4],
                },
                "dispatch_brain": {
                    "preferred_posture": _text((raw.get("dispatch_brain", {}) or {}).get("preferred_posture")) or "balanced",
                    "cash_posture": _text((raw.get("dispatch_brain", {}) or {}).get("cash_posture")) or "hold_buffer",
                    "tactical_bias": _text((raw.get("dispatch_brain", {}) or {}).get("tactical_bias")) or "reduce_risk_first",
                    "decision_basis": [_text(item) for item in list((raw.get("dispatch_brain", {}) or {}).get("decision_basis", []) or []) if _text(item)][:4],
                },
                "operations_brain": {
                    "watch_items": [_text(item) for item in list((raw.get("operations_brain", {}) or {}).get("watch_items", []) or []) if _text(item)][:8],
                    "incident_actions": [_text(item) for item in list((raw.get("operations_brain", {}) or {}).get("incident_actions", []) or []) if _text(item)][:6],
                    "decision_basis": [_text(item) for item in list((raw.get("operations_brain", {}) or {}).get("decision_basis", []) or []) if _text(item)][:4],
                },
                "uncertainty_flags": [_text(item) for item in list(raw.get("uncertainty_flags", []) or []) if _text(item)][:6],
                "overfit_guard": _text(raw.get("overfit_guard"))[:240],
                "error": "",
            }
        else:
            normalized = _default_operating_brain(_text(result.get("error_message")) or "llm_operating_brain_failed")
    except Exception as exc:
        normalized = _default_operating_brain(str(exc))

    artifact_root = ensure_dir(Path(_text(cfg.get("artifact_root")) or (Path(__file__).resolve().parents[3] / "data" / "trade_clock" / "llm_operating_brain")).resolve())
    latest_root = ensure_dir(artifact_root / "latest")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload_out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "review": normalized,
        "input_payload": payload,
    }
    artifact_path = artifact_root / f"llm_operating_brain_{stamp}.json"
    latest_path = latest_root / "latest_llm_operating_brain.json"
    artifact_path.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    payload_out["trace"] = write_llm_trace(
        config=config,
        stage="llm_operating_brain",
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        result=result,
        normalized_review=normalized,
        input_payload={"candidate_rows": candidate_rows, "alpha_lifecycle": alpha_items},
        meta={"candidate_count": len(candidate_rows)},
    )
    payload_out["artifact_path"] = str(artifact_path)
    payload_out["latest_artifact_path"] = str(latest_path)
    return payload_out
