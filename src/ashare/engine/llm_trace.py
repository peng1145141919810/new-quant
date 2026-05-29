from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .config_utils import ensure_dir


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _root(config: Dict[str, Any]) -> Path:
    cfg = dict(config.get("llm_trace", {}) or {})
    default_root = Path(__file__).resolve().parents[3] / "data" / "trade_clock" / "llm_trace"
    return ensure_dir(Path(_text(cfg.get("artifact_root")) or default_root).resolve())


def write_llm_trace(
    *,
    config: Dict[str, Any],
    stage: str,
    provider: str,
    system_prompt: str,
    user_prompt: str,
    result: Dict[str, Any],
    normalized_review: Dict[str, Any],
    input_payload: Dict[str, Any] | None = None,
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    cfg = dict(config.get("llm_trace", {}) or {})
    if not bool(cfg.get("enabled", True)):
        return {"enabled": False, "applied": False}
    prompt_char_cap = max(_safe_int(cfg.get("prompt_char_cap", 4000), 4000), 256)
    raw_char_cap = max(_safe_int(cfg.get("raw_response_char_cap", 4000), 4000), 256)
    store_prompts = bool(cfg.get("store_prompts", True))
    store_raw_response = bool(cfg.get("store_raw_response", True))
    now = datetime.now()
    root = _root(config)
    latest_root = ensure_dir(root / "latest")
    stamp = now.strftime("%Y%m%d_%H%M%S")
    safe_stage = _text(stage).replace("\\", "_").replace("/", "_").replace(" ", "_") or "unknown_stage"
    response_payload = dict(result.get("data", {}) or {})
    raw_model_result = {
        "ok": bool(result.get("ok", False)),
        "provider": _text(result.get("provider") or provider),
        "model": _text(result.get("model")),
        "response_id": _text(result.get("response_id")),
        "status_code": result.get("status_code"),
        "error_type": _text(result.get("error_type")),
        "error_message": _text(result.get("error_message"))[:raw_char_cap],
        "elapsed_seconds": float(result.get("elapsed_seconds", 0.0) or 0.0),
        "output_chars": int(result.get("output_chars", 0) or 0),
        "raw_response_preview": json.dumps(response_payload, ensure_ascii=False)[:raw_char_cap] if store_raw_response else "",
    }
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "stage": safe_stage,
        "provider": _text(provider),
        "prompt_hash": {
            "system_prompt_sha256": _sha256(system_prompt),
            "user_prompt_sha256": _sha256(user_prompt),
        },
        "prompt_preview": {
            "system_prompt": system_prompt[:prompt_char_cap] if store_prompts else "",
            "user_prompt": user_prompt[:prompt_char_cap] if store_prompts else "",
        },
        "input_payload": dict(input_payload or {}),
        "normalized_review": dict(normalized_review or {}),
        "decision_log": {
            "decision_basis": list(dict(normalized_review or {}).get("decision_basis", []) or []),
            "uncertainty_flags": list(dict(normalized_review or {}).get("uncertainty_flags", []) or []),
            "overfit_guard": _text(dict(normalized_review or {}).get("overfit_guard")),
        },
        "raw_model_result": raw_model_result,
        "meta": dict(meta or {}),
    }
    artifact_path = root / f"{safe_stage}_{stamp}.json"
    latest_path = latest_root / f"latest_{safe_stage}.json"
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "enabled": True,
        "applied": True,
        "artifact_path": str(artifact_path),
        "latest_artifact_path": str(latest_path),
        "prompt_hash": payload["prompt_hash"],
    }
