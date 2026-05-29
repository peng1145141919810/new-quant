from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from ..config_utils import ensure_dir
from ..logging_utils import log_line
from ..trading_clock import clock_now
from .contracts import MARKET_STATE_FIELDS, default_market_state_payload
from .core.feature_builder import build_market_feature_snapshot
from .core.scorer import compute_market_scores
from .policy.regime_policy import build_regime_policy


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _market_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("market_state", {}) or {})


def _output_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("market_state_root", "") or "")).resolve())


def _config_path(config: Dict[str, Any]) -> Path:
    cfg = _market_cfg(config)
    return Path(str(cfg.get("config_path", "") or "")).resolve()


def _load_runtime_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _config_path(config)
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def _upsert_daily(path: Path, row: Dict[str, Any]) -> None:
    current = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=MARKET_STATE_FIELDS)
    incoming = pd.DataFrame([row])
    for col in MARKET_STATE_FIELDS:
        if col not in current.columns:
            current[col] = pd.NA
        if col not in incoming.columns:
            incoming[col] = pd.NA
    merged = pd.concat([current, incoming], ignore_index=True)
    merged["date"] = merged["date"].astype(str).str.slice(0, 10)
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    merged = merged[MARKET_STATE_FIELDS].copy()
    merged.to_csv(path, index=False, encoding="utf-8-sig")


def build_market_state_artifacts(config: Dict[str, Any]) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    output_root = _output_root(config)
    daily_path = output_root / "market_state_daily.csv"
    latest_path = output_root / "latest_market_state.json"
    explainer_path = output_root / "market_state_explainer.json"
    generated_at = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    if not bool(market_cfg.get("enabled", True)):
        payload = default_market_state_payload(now_text=generated_at)
        payload["de_risk_hint"] = "market_state_disabled"
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _upsert_daily(daily_path, payload)
        return {"ok": True, "status": "disabled", "latest_path": str(latest_path), "daily_path": str(daily_path), "payload": payload}

    runtime_payload = _load_runtime_payload(config)
    feature_snapshot = build_market_feature_snapshot(config=config, output_root=output_root)
    if not bool(feature_snapshot.get("ok", False)):
        payload = default_market_state_payload(now_text=generated_at)
        payload["de_risk_hint"] = str(feature_snapshot.get("reason", "feature_snapshot_unavailable"))
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _upsert_daily(daily_path, payload)
        return {
            "ok": False,
            "status": "fallback",
            "reason": str(feature_snapshot.get("reason", "feature_snapshot_unavailable")),
            "latest_path": str(latest_path),
            "daily_path": str(daily_path),
            "payload": payload,
        }

    if not bool(market_cfg.get("use_router_bias", True)):
        feature_snapshot["mechanism_scores"] = {}
    scores = compute_market_scores(feature_snapshot=feature_snapshot, config_payload=runtime_payload)
    policy = build_regime_policy(scores=scores, feature_snapshot=feature_snapshot, config_payload=runtime_payload)
    payload = default_market_state_payload(now_text=generated_at, trade_date=str(feature_snapshot.get("date", "") or ""))
    payload.update(dict(feature_snapshot.get("metrics", {}) or {}))
    payload.update({key: value for key, value in scores.items() if key != "mechanism_scores"})
    payload.update({key: value for key, value in policy.items() if key != "mechanism_multipliers"})
    payload["mechanism_lead_score"] = float(max(dict(scores.get("mechanism_scores", {}) or {}).values()) if dict(scores.get("mechanism_scores", {}) or {}) else 0.0)
    payload["mechanism_multipliers"] = dict(policy.get("mechanism_multipliers", {}) or {})
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _upsert_daily(daily_path, payload)

    explainer = {
        "generated_at": generated_at,
        "date": payload.get("date", ""),
        "feature_snapshot": feature_snapshot,
        "scores": scores,
        "policy": policy,
        "config_path": str(_config_path(config)),
    }
    explainer_path.write_text(json.dumps(explainer, ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(
        config,
        (
            "Market State: 完成 "
            f"date={payload.get('date', '')} regime={payload.get('market_regime', '')} "
            f"risk_budget={float(payload.get('risk_budget_multiplier', 1.0) or 1.0):.2f} "
            f"turnover={float(payload.get('turnover_multiplier', 1.0) or 1.0):.2f}"
        ),
    )
    return {
        "ok": True,
        "status": "ok",
        "latest_path": str(latest_path),
        "daily_path": str(daily_path),
        "explainer_path": str(explainer_path),
        "payload": payload,
        "mechanism_multipliers": dict(policy.get("mechanism_multipliers", {}) or {}),
    }


def load_latest_market_state(config: Dict[str, Any], allow_build: bool = False) -> Dict[str, Any]:
    latest_path = _output_root(config) / "latest_market_state.json"
    if latest_path.exists():
        try:
            payload = _load_json(latest_path)
            if payload:
                return payload
        except Exception:
            pass
    if allow_build:
        result = build_market_state_artifacts(config=config)
        return dict(result.get("payload", {}) or {})
    now_text = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    return default_market_state_payload(now_text=now_text)
