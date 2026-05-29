from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .config_utils import ensure_dir
from .execution_bridge_runner import run_execution_health_probe
from .sql_store import (
    append_runtime_jsonl_record,
    ensure_schema,
    load_runtime_json_artifact,
    resolve_sqlite_path,
    sql_store_enabled,
    sqlite_connection,
    upsert_runtime_json_artifact,
)
from .trading_clock import clock_now

SYSTEM_NORMAL = "NORMAL"
SYSTEM_DEGRADED = "DEGRADED"
SYSTEM_HALT = "HALT"

MARKET_NORMAL = "NORMAL"
MARKET_CAUTION = "CAUTION"
MARKET_PANIC = "PANIC"


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
    return path


def _dump_json(path: Path, payload: Dict[str, Any]) -> Path:
    config = getattr(_dump_json, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            upsert_runtime_json_artifact(conn, path, payload)
    return _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> Path:
    config = getattr(_append_jsonl, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            append_runtime_jsonl_record(conn, path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def _parse_iso(text: str) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _seconds_since(text: str, now: datetime) -> float | None:
    parsed = _parse_iso(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    elif parsed.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=parsed.tzinfo)
    return max((now - parsed).total_seconds(), 0.0)


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve())


def _live_execution_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("live_execution_root", "") or "")).resolve())


def _paths(config: Dict[str, Any]) -> Dict[str, Path]:
    root = _trade_clock_root(config)
    return {
        "root": root,
        "manual_overrides": root / "manual_overrides.json",
        "manual_override_history": root / "manual_override_history.jsonl",
        "system_state": root / "system_safety_state.json",
        "incident_log": root / "incident_log.jsonl",
        "latest_account_health": root / "latest_account_health.json",
        "health_probe_root": ensure_dir(root / "health_probes"),
        "latest_dispatch": root / "latest_execution_dispatch.json",
    }


def _safety_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("safety", {}) or {})


def _default_manual_overrides(now_text: str = "") -> Dict[str, Any]:
    return {
        "updated_at": str(now_text or ""),
        "updated_by": "operator",
        "manual_halt": False,
        "manual_reduce_only": False,
        "note": "",
    }


def load_manual_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    _dump_json._active_config = config
    _append_jsonl._active_config = config
    path = _paths(config)["manual_overrides"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if isinstance(payload, dict) and payload:
                    normalized = {
                        "updated_at": str(payload.get("updated_at", "") or ""),
                        "updated_by": str(payload.get("updated_by", "operator") or "operator"),
                        "manual_halt": bool(payload.get("manual_halt", False)),
                        "manual_reduce_only": bool(payload.get("manual_reduce_only", False)),
                        "note": str(payload.get("note", "") or ""),
                    }
                    if normalized["updated_at"]:
                        return normalized
            except Exception:
                pass
    if not path.exists():
        payload = _default_manual_overrides(
            clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).isoformat(timespec="seconds")
        )
        _dump_json(path, payload)
        return payload
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = _default_manual_overrides(
            clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).isoformat(timespec="seconds")
        )
        _dump_json(path, payload)
        return payload
    normalized = {
        "updated_at": str(payload.get("updated_at", "") or ""),
        "updated_by": str(payload.get("updated_by", "operator") or "operator"),
        "manual_halt": bool(payload.get("manual_halt", False)),
        "manual_reduce_only": bool(payload.get("manual_reduce_only", False)),
        "note": str(payload.get("note", "") or ""),
    }
    if not normalized["updated_at"]:
        normalized["updated_at"] = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).isoformat(timespec="seconds")
        _dump_json(path, normalized)
    return normalized


def _default_system_state(now_text: str = "") -> Dict[str, Any]:
    return {
        "updated_at": str(now_text or ""),
        "service_name": "",
        "current_mode": "",
        "system_mode": SYSTEM_NORMAL,
        "market_safety_regime": MARKET_NORMAL,
        "manual_halt": False,
        "manual_reduce_only": False,
        "current_gate_status": "closed",
        "gate_open": False,
        "gate_reason": "",
        "latest_release_id": "",
        "latest_release_time": "",
        "latest_release_validation": {"ok": False, "status": "missing", "errors": []},
        "last_successful_account_check": "",
        "last_successful_position_sync": "",
        "last_successful_execution": "",
        "last_incident_level": "",
        "last_incident_type": "",
        "incident_signatures": {},
        "degraded_reason": "",
        "halt_reason": "",
        "release_age_seconds": None,
        "account_state_age_seconds": None,
        "position_sync_age_seconds": None,
        "effective_reduce_only": False,
        "effective_turnover_multiplier": 1.0,
        "allow_unfinished_orders_reconcile": False,
        "unfinished_orders_reconcile_allowed": False,
    }


def load_system_safety_state(config: Dict[str, Any]) -> Dict[str, Any]:
    _dump_json._active_config = config
    _append_jsonl._active_config = config
    path = _paths(config)["system_state"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if isinstance(payload, dict) and payload:
                    base = _default_system_state()
                    base.update(payload)
                    return base
            except Exception:
                pass
    if not path.exists():
        return _default_system_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_system_state()
    base = _default_system_state()
    base.update(payload)
    return base


def save_system_safety_state(config: Dict[str, Any], state: Dict[str, Any]) -> Path:
    _dump_json._active_config = config
    return _dump_json(_paths(config)["system_state"], state)


def record_incident(
    config: Dict[str, Any],
    incident_type: str,
    severity: str,
    component: str,
    reason: str,
    action_taken: str,
    requires_human_action: bool,
    before_system_mode: str,
    after_system_mode: str,
    before_market_regime: str,
    after_market_regime: str,
    context_snapshot_ref: str = "",
) -> Dict[str, Any]:
    now_text = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).isoformat(timespec="seconds")
    payload = {
        "incident_time": now_text,
        "incident_type": str(incident_type or ""),
        "severity": str(severity or "warning"),
        "component": str(component or ""),
        "before_system_mode": str(before_system_mode or ""),
        "after_system_mode": str(after_system_mode or ""),
        "before_market_regime": str(before_market_regime or ""),
        "after_market_regime": str(after_market_regime or ""),
        "reason": str(reason or ""),
        "action_taken": str(action_taken or ""),
        "requires_human_action": bool(requires_human_action),
        "context_snapshot_ref": str(context_snapshot_ref or ""),
    }
    _append_jsonl(_paths(config)["incident_log"], payload)
    return payload


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_release_artifacts(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    artifacts = dict(release_doc.get("artifacts", {}) or {})
    checksums = dict(release_doc.get("checksums", {}) or {})
    manifest_path = Path(str(artifacts.get("manifest_path", "") or "")).resolve()
    target_path = Path(str(artifacts.get("target_positions_path", "") or "")).resolve()
    summary_path = Path(str(artifacts.get("portfolio_summary_path", "") or "")).resolve()
    if not manifest_path.exists():
        errors.append(f"manifest_missing:{manifest_path}")
    if not target_path.exists():
        errors.append(f"target_positions_missing:{target_path}")
    if not summary_path.exists():
        errors.append(f"portfolio_summary_missing:{summary_path}")
    target_count_verified = 0
    if target_path.exists():
        try:
            target_df = pd.read_csv(target_path)
            target_count_verified = int(len(target_df.index))
            if target_df.empty:
                errors.append("target_positions_empty")
        except Exception as exc:
            errors.append(f"target_positions_read_failed:{exc}")
    if target_path.exists() and str(checksums.get("target_positions_sha256", "") or "").strip():
        current = _sha256_of_file(target_path)
        if current != str(checksums.get("target_positions_sha256", "") or ""):
            errors.append("target_positions_checksum_mismatch")
    if summary_path.exists() and str(checksums.get("portfolio_summary_sha256", "") or "").strip():
        current = _sha256_of_file(summary_path)
        if current != str(checksums.get("portfolio_summary_sha256", "") or ""):
            errors.append("portfolio_summary_checksum_mismatch")
    return {
        "ok": not errors,
        "status": "ok" if not errors else "invalid",
        "errors": errors,
        "manifest_path": str(manifest_path),
        "target_positions_path": str(target_path),
        "portfolio_summary_path": str(summary_path),
        "target_count_verified": target_count_verified,
    }


def _load_price_snapshot(config: Dict[str, Any]) -> pd.DataFrame:
    price_path = Path(str(config.get("market_pipeline", {}).get("price_snapshot_path", "") or "")).resolve()
    if not price_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(price_path)
    except Exception:
        return pd.DataFrame()


def _load_hs300(config: Dict[str, Any]) -> pd.DataFrame:
    hs300_path = Path(str(config.get("market_pipeline", {}).get("hs300_path", "") or "")).resolve()
    if not hs300_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(hs300_path)
    except Exception:
        return pd.DataFrame()


def assess_market_safety_regime(config: Dict[str, Any]) -> Dict[str, Any]:
    safety_cfg = _safety_cfg(config)
    snapshot = _load_price_snapshot(config)
    hs300 = _load_hs300(config)
    if snapshot.empty or "pct_chg" not in snapshot.columns:
        return {
            "ok": False,
            "regime": MARKET_CAUTION,
            "reason": "price_snapshot_unavailable",
            "snapshot_date": "",
            "metrics": {},
        }
    snapshot = snapshot.copy()
    snapshot["pct_chg"] = pd.to_numeric(snapshot["pct_chg"], errors="coerce")
    snapshot = snapshot.dropna(subset=["pct_chg"])
    if snapshot.empty:
        return {
            "ok": False,
            "regime": MARKET_CAUTION,
            "reason": "price_snapshot_pct_chg_missing",
            "snapshot_date": "",
            "metrics": {},
        }
    latest_date = ""
    if "date" in snapshot.columns and len(snapshot.index) > 0:
        latest_date = str(snapshot["date"].astype(str).iloc[-1])
    mean_pct = float(snapshot["pct_chg"].mean())
    median_pct = float(snapshot["pct_chg"].median())
    limit_down_ratio = float((snapshot["pct_chg"] <= -9.5).mean()) if len(snapshot.index) else 0.0
    broad_down_ratio = float((snapshot["pct_chg"] <= -7.0).mean()) if len(snapshot.index) else 0.0
    hs300_return_pct = 0.0
    if not hs300.empty and "close" in hs300.columns and len(hs300.index) >= 2:
        close_now = float(pd.to_numeric(hs300["close"], errors="coerce").iloc[-1])
        close_prev = float(pd.to_numeric(hs300["close"], errors="coerce").iloc[-2])
        if close_prev > 0:
            hs300_return_pct = (close_now / close_prev - 1.0) * 100.0
    reason_parts: List[str] = []
    regime = MARKET_NORMAL
    if (
        mean_pct <= float(safety_cfg.get("market_panic_mean_pct_chg", -2.2))
        or hs300_return_pct <= float(safety_cfg.get("market_panic_hs300_return_pct", -3.0))
        or broad_down_ratio >= float(safety_cfg.get("market_panic_limit_down_ratio", 0.12))
    ):
        regime = MARKET_PANIC
        reason_parts.append("panic_threshold_hit")
    elif (
        mean_pct <= float(safety_cfg.get("market_caution_mean_pct_chg", -1.0))
        or hs300_return_pct <= float(safety_cfg.get("market_caution_hs300_return_pct", -1.5))
        or broad_down_ratio >= float(safety_cfg.get("market_caution_limit_down_ratio", 0.05))
    ):
        regime = MARKET_CAUTION
        reason_parts.append("caution_threshold_hit")
    else:
        reason_parts.append("market_normal")
    return {
        "ok": True,
        "regime": regime,
        "reason": ",".join(reason_parts),
        "snapshot_date": latest_date,
        "metrics": {
            "market_mean_pct_chg": round(mean_pct, 4),
            "market_median_pct_chg": round(median_pct, 4),
            "market_limit_down_ratio": round(limit_down_ratio, 4),
            "market_broad_down_ratio": round(broad_down_ratio, 4),
            "hs300_return_pct": round(hs300_return_pct, 4),
            "n_names": int(len(snapshot.index)),
        },
    }


def _load_account_health_from_disk(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _paths(config)["latest_account_health"]
    if not path.exists():
        return {"ok": False, "status": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "status": "read_failed", "error": str(exc)}
    payload["status"] = "cached"
    return payload


def probe_account_health(
    config: Dict[str, Any],
    project_root: Path,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    safety_cfg = _safety_cfg(config)
    now = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    cached = _load_account_health_from_disk(config)
    cached_age = _seconds_since(str(cached.get("timestamp", "") or ""), now) if cached else None
    refresh_interval = int(safety_cfg.get("health_probe_interval_seconds", 300) or 300)
    should_refresh = bool(force_refresh or not bool(cached.get("ok", False)) or cached_age is None or cached_age > refresh_interval)
    if not should_refresh and bool(cached.get("ok", False)):
        cached["age_seconds"] = cached_age
        return cached
    try:
        report = run_execution_health_probe(config=config, project_root=project_root)
    except Exception as exc:
        if bool(cached.get("ok", False)):
            cached["status"] = "stale_cache_after_probe_failure"
            cached["probe_error"] = str(exc)
            cached["age_seconds"] = cached_age
            return cached
        return {
            "ok": False,
            "status": "probe_failed",
            "error": str(exc),
        }
    if not bool(report.get("ok", False)):
        if bool(cached.get("ok", False)):
            cached["status"] = "stale_cache_after_probe_error"
            cached["probe_error"] = str(report.get("error", "") or report.get("stdout", "") or "")
            cached["age_seconds"] = cached_age
            return cached
        return report
    report["age_seconds"] = _seconds_since(str(report.get("timestamp", "") or ""), now)
    report["status"] = "fresh"
    _dump_json(_paths(config)["latest_account_health"], report)
    history_path = _paths(config)["health_probe_root"] / f"account_health_{now.strftime('%Y%m%d_%H%M%S')}.json"
    _dump_json(history_path, report)
    report["history_path"] = str(history_path)
    return report


def _load_latest_execution_feedback(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _live_execution_root(config) / "latest_execution_feedback.json"
    if not path.exists():
        return {"ok": False, "status": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "status": "read_failed", "error": str(exc)}
    summary = dict(payload.get("summary", {}) or {})
    total = int(summary.get("n_success", 0) or 0) + int(summary.get("n_partial", 0) or 0) + int(summary.get("n_failed", 0) or 0)
    fail_ratio = (float(summary.get("n_failed", 0) or 0) / total) if total > 0 else 0.0
    return {
        "ok": True,
        "status": "ok",
        "generated_at": str(payload.get("generated_at", "") or ""),
        "summary": summary,
        "total_orders": total,
        "fail_ratio": fail_ratio,
    }


def apply_execution_safety_overrides(config: Dict[str, Any], safety_report: Dict[str, Any]) -> Dict[str, Any]:
    updated = copy.deepcopy(config)
    portfolio_control = dict(updated.get("portfolio_control", {}) or {})
    turnover_multiplier = float(safety_report.get("effective_turnover_multiplier", 1.0) or 1.0)
    current_turnover = float(portfolio_control.get("max_daily_turnover_ratio", 0.25) or 0.25)
    portfolio_control["max_daily_turnover_ratio"] = round(max(current_turnover * turnover_multiplier, 0.0), 6)
    portfolio_control["reduce_only"] = bool(safety_report.get("effective_reduce_only", False))
    updated["portfolio_control"] = portfolio_control
    updated["safety_runtime"] = {
        "system_mode": str(safety_report.get("system_mode", "") or ""),
        "market_safety_regime": str(safety_report.get("market_safety_regime", "") or ""),
        "manual_halt": bool(safety_report.get("manual_halt", False)),
        "manual_reduce_only": bool(safety_report.get("manual_reduce_only", False)),
        "effective_reduce_only": bool(safety_report.get("effective_reduce_only", False)),
        "effective_turnover_multiplier": float(safety_report.get("effective_turnover_multiplier", 1.0) or 1.0),
        "panic_reduce_only_ignored": bool(safety_report.get("panic_reduce_only_ignored", False)),
        "allow_unfinished_orders_reconcile": bool(safety_report.get("allow_unfinished_orders_reconcile", False)),
        "unfinished_orders_reconcile_allowed": bool(safety_report.get("unfinished_orders_reconcile_allowed", False)),
    }
    return updated


def assess_system_safety(
    config: Dict[str, Any],
    gate: Dict[str, Any],
    project_root: Path,
    service_name: str,
    current_mode: str,
    force_account_refresh: bool = False,
) -> Dict[str, Any]:
    _dump_json._active_config = config
    _append_jsonl._active_config = config
    timezone_name = str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
    now = clock_now(timezone_name)
    if not bool(_safety_cfg(config).get("enabled", True)):
        state = _default_system_state(now.isoformat(timespec="seconds"))
        state.update(
            {
                "updated_at": now.isoformat(timespec="seconds"),
                "service_name": str(service_name or ""),
                "current_mode": str(current_mode or ""),
                "current_gate_status": "open" if bool(gate.get("should_execute", False)) else "closed",
                "gate_open": bool(gate.get("should_execute", False)),
                "gate_reason": str(gate.get("reason", "") or ""),
            }
        )
        save_system_safety_state(config=config, state=state)
        return {
            "ok": True,
            "allow_execution": bool(gate.get("should_execute", False)),
            "system_mode": SYSTEM_NORMAL,
            "market_safety_regime": MARKET_NORMAL,
            "manual_halt": False,
            "manual_reduce_only": False,
            "effective_reduce_only": False,
            "effective_turnover_multiplier": 1.0,
            "state": state,
            "incidents": [],
            "release_validation": {"ok": True, "status": "skipped", "errors": []},
            "account_health": {"ok": True, "status": "skipped"},
            "market": {"ok": True, "regime": MARKET_NORMAL, "reason": "safety_disabled"},
        }
    prev_state = load_system_safety_state(config)
    overrides = load_manual_overrides(config)
    market = assess_market_safety_regime(config)
    release_doc: Dict[str, Any] = {}
    release_validation = {"ok": False, "status": "missing", "errors": ["release_missing"]}
    release_info = dict(gate.get("release", {}) or {})
    manifest_path = Path(str(release_info.get("manifest_path", "") or "")).resolve()
    if manifest_path.exists():
        try:
            release_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            release_validation = validate_release_artifacts(release_doc)
        except Exception as exc:
            release_validation = {"ok": False, "status": "read_failed", "errors": [str(exc)]}
    account_health = probe_account_health(config=config, project_root=project_root, force_refresh=force_account_refresh)
    feedback_health = _load_latest_execution_feedback(config)
    feedback_summary = dict(feedback_health.get("summary", {}) or {})

    system_mode = SYSTEM_NORMAL
    degraded_reason = ""
    halt_reason = ""
    account_age = _seconds_since(str(account_health.get("timestamp", "") or ""), now)
    position_age = account_age
    release_age = _seconds_since(str(release_doc.get("generated_at", "") or ""), now)
    safety_cfg = _safety_cfg(config)
    execution_policy = dict(config.get("execution_policy", {}) or {})
    account_mode = str(execution_policy.get("account_mode", "simulation") or "simulation").strip().lower()
    panic_reduce_only_ignored = bool(execution_policy.get("ignore_market_panic_reduce_only", False))
    allow_unfinished_orders_reconcile = bool(execution_policy.get("allow_unfinished_orders_reconcile", False))
    unfinished_orders_reconcile_allowed = False
    if account_mode != "precision":
        account_health = {
            "ok": True,
            "status": "skipped_simulation_mode",
            "timestamp": now.isoformat(timespec="seconds"),
            "positions_count": 0,
            "order_health": {"summary": {}},
        }
        account_age = 0.0
        position_age = 0.0

    if bool(overrides.get("manual_halt", False)):
        system_mode = SYSTEM_HALT
        halt_reason = "manual_halt"
    elif not bool(release_validation.get("ok", False)):
        system_mode = SYSTEM_HALT
        halt_reason = "release_validation_failed"
    elif not bool(gate.get("ok", False)):
        system_mode = SYSTEM_HALT
        halt_reason = str(gate.get("reason", "") or "gate_error")
    elif not bool(account_health.get("ok", False)):
        system_mode = SYSTEM_HALT
        halt_reason = str(account_health.get("status", "") or "account_health_unknown")
    elif account_age is None or account_age > float(safety_cfg.get("account_state_max_age_seconds", 900) or 900):
        system_mode = SYSTEM_HALT
        halt_reason = "account_state_stale"
    elif position_age is None or position_age > float(safety_cfg.get("position_sync_max_age_seconds", 900) or 900):
        system_mode = SYSTEM_HALT
        halt_reason = "position_state_stale"
    elif release_age is None or release_age > float(safety_cfg.get("release_max_age_seconds", 172800) or 172800):
        system_mode = SYSTEM_HALT
        halt_reason = "release_stale"
    elif bool(safety_cfg.get("fail_on_unknown_order_status", True)) and int(account_health.get("order_health", {}).get("summary", {}).get("n_unknown_status_orders", 0) or 0) > 0:
        system_mode = SYSTEM_HALT
        halt_reason = "unknown_order_status"
    elif bool(safety_cfg.get("fail_on_unfinished_orders", True)) and int(account_health.get("order_health", {}).get("summary", {}).get("n_unfinished_orders", 0) or 0) > 0:
        if allow_unfinished_orders_reconcile:
            unfinished_orders_reconcile_allowed = True
        else:
            system_mode = SYSTEM_HALT
            halt_reason = "unfinished_orders_present"
    elif bool(feedback_health.get("ok", False)) and int(feedback_health.get("total_orders", 0) or 0) >= int(safety_cfg.get("execution_fail_min_orders", 3) or 3):
        fail_ratio = float(feedback_health.get("fail_ratio", 0.0) or 0.0)
        if fail_ratio >= float(safety_cfg.get("execution_fail_ratio_halt", 0.75) or 0.75):
            system_mode = SYSTEM_HALT
            halt_reason = f"recent_execution_fail_ratio={fail_ratio:.3f}"
        elif fail_ratio >= float(safety_cfg.get("execution_fail_ratio_degraded", 0.35) or 0.35):
            system_mode = SYSTEM_DEGRADED
            degraded_reason = f"recent_execution_fail_ratio={fail_ratio:.3f}"
    elif not bool(market.get("ok", False)):
        system_mode = SYSTEM_DEGRADED
        degraded_reason = str(market.get("reason", "") or "market_snapshot_unknown")

    market_regime = str(market.get("regime", MARKET_NORMAL) or MARKET_NORMAL)
    effective_reduce_only = bool(overrides.get("manual_reduce_only", False))
    if market_regime == MARKET_PANIC and not panic_reduce_only_ignored:
        effective_reduce_only = True
    if system_mode == SYSTEM_DEGRADED and bool(safety_cfg.get("degraded_reduce_only", True)):
        effective_reduce_only = True
    turnover_multiplier = 1.0
    if market_regime == MARKET_CAUTION:
        turnover_multiplier = min(turnover_multiplier, float(safety_cfg.get("caution_turnover_multiplier", 0.5) or 0.5))
    if system_mode == SYSTEM_DEGRADED:
        turnover_multiplier = min(turnover_multiplier, float(safety_cfg.get("caution_turnover_multiplier", 0.5) or 0.5))
    current_gate_status = "open" if bool(gate.get("should_execute", False)) else ("error" if not bool(gate.get("ok", False)) else "closed")
    allow_execution = bool(gate.get("should_execute", False)) and system_mode != SYSTEM_HALT and not bool(overrides.get("manual_halt", False))

    latest_execution_time = ""
    latest_dispatch = _paths(config)["latest_dispatch"]
    if latest_dispatch.exists():
        try:
            latest_execution_time = str(json.loads(latest_dispatch.read_text(encoding="utf-8")).get("timestamp", "") or "")
        except Exception:
            latest_execution_time = ""

    state = _default_system_state(now.isoformat(timespec="seconds"))
    state.update(
        {
            "updated_at": now.isoformat(timespec="seconds"),
            "service_name": str(service_name or ""),
            "current_mode": str(current_mode or ""),
            "system_mode": system_mode,
            "market_safety_regime": market_regime,
            "manual_halt": bool(overrides.get("manual_halt", False)),
            "manual_reduce_only": bool(overrides.get("manual_reduce_only", False)),
            "current_gate_status": current_gate_status,
            "gate_open": bool(gate.get("should_execute", False)),
            "gate_reason": str(gate.get("reason", "") or ""),
            "latest_release_id": str(release_info.get("release_id", "") or ""),
            "latest_release_time": str(release_doc.get("generated_at", "") or ""),
            "latest_release_validation": release_validation,
            "last_successful_account_check": str(account_health.get("timestamp", "") or "") if bool(account_health.get("ok", False)) else str(prev_state.get("last_successful_account_check", "") or ""),
            "last_successful_position_sync": str(account_health.get("timestamp", "") or "") if bool(account_health.get("ok", False)) else str(prev_state.get("last_successful_position_sync", "") or ""),
            "last_successful_execution": latest_execution_time or str(prev_state.get("last_successful_execution", "") or ""),
            "degraded_reason": degraded_reason,
            "halt_reason": halt_reason,
            "release_age_seconds": release_age,
            "account_state_age_seconds": account_age,
            "position_sync_age_seconds": position_age,
            "effective_reduce_only": effective_reduce_only,
            "effective_turnover_multiplier": turnover_multiplier,
            "panic_reduce_only_ignored": panic_reduce_only_ignored,
            "allow_unfinished_orders_reconcile": allow_unfinished_orders_reconcile,
            "unfinished_orders_reconcile_allowed": unfinished_orders_reconcile_allowed,
            "market_snapshot": market,
            "account_health": {
                "status": str(account_health.get("status", "") or ""),
                "timestamp": str(account_health.get("timestamp", "") or ""),
                "age_seconds": account_age,
                "positions_count": int(account_health.get("positions_count", 0) or 0),
                "n_day_orders": int(account_health.get("order_health", {}).get("summary", {}).get("n_day_orders", 0) or 0),
                "n_unfinished_orders": int(account_health.get("order_health", {}).get("summary", {}).get("n_unfinished_orders", 0) or 0),
                "n_unknown_status_orders": int(account_health.get("order_health", {}).get("summary", {}).get("n_unknown_status_orders", 0) or 0),
                "error": str(account_health.get("error", "") or account_health.get("probe_error", "") or ""),
            },
            "execution_feedback_health": {
                "status": str(feedback_health.get("status", "") or ""),
                "generated_at": str(feedback_health.get("generated_at", "") or ""),
                "fail_ratio": float(feedback_health.get("fail_ratio", 0.0) or 0.0),
                "total_orders": int(feedback_health.get("total_orders", 0) or 0),
                "summary": feedback_summary,
            },
        }
    )

    incidents: List[Dict[str, Any]] = []
    incident_signatures = dict(prev_state.get("incident_signatures", {}) or {})
    context_ref = str(_paths(config)["system_state"])
    if bool(prev_state.get("manual_halt", False)) != bool(overrides.get("manual_halt", False)) or bool(prev_state.get("manual_reduce_only", False)) != bool(overrides.get("manual_reduce_only", False)):
        signature = f"manual_override_changed|{bool(overrides.get('manual_halt', False))}|{bool(overrides.get('manual_reduce_only', False))}|{str(overrides.get('note', '') or '')}"
        change = {
            "updated_at": state["updated_at"],
            "before": {
                "manual_halt": bool(prev_state.get("manual_halt", False)),
                "manual_reduce_only": bool(prev_state.get("manual_reduce_only", False)),
            },
            "after": {
                "manual_halt": bool(overrides.get("manual_halt", False)),
                "manual_reduce_only": bool(overrides.get("manual_reduce_only", False)),
            },
            "note": str(overrides.get("note", "") or ""),
        }
        _append_jsonl(_paths(config)["manual_override_history"], change)
        if str(incident_signatures.get("manual_override_changed", "") or "") != signature:
            incidents.append(
                record_incident(
                    config=config,
                    incident_type="manual_override_changed",
                    severity="warning",
                    component="safety_guard",
                    reason=str(overrides.get("note", "") or "manual_override_changed"),
                    action_taken="state_updated",
                    requires_human_action=False,
                    before_system_mode=str(prev_state.get("system_mode", SYSTEM_NORMAL) or SYSTEM_NORMAL),
                    after_system_mode=system_mode,
                    before_market_regime=str(prev_state.get("market_safety_regime", MARKET_NORMAL) or MARKET_NORMAL),
                    after_market_regime=market_regime,
                    context_snapshot_ref=context_ref,
                )
            )
            incident_signatures["manual_override_changed"] = signature
    if str(prev_state.get("market_safety_regime", MARKET_NORMAL) or MARKET_NORMAL) != market_regime:
        signature = f"market_regime_changed|{market_regime}|{str(market.get('reason', '') or '')}"
        if str(incident_signatures.get("market_regime_changed", "") or "") != signature:
            incidents.append(
                record_incident(
                    config=config,
                    incident_type="market_regime_changed",
                    severity="error" if market_regime == MARKET_PANIC else "warning",
                    component="safety_guard",
                    reason=str(market.get("reason", "") or ""),
                    action_taken="risk_posture_updated",
                    requires_human_action=market_regime == MARKET_PANIC,
                    before_system_mode=str(prev_state.get("system_mode", SYSTEM_NORMAL) or SYSTEM_NORMAL),
                    after_system_mode=system_mode,
                    before_market_regime=str(prev_state.get("market_safety_regime", MARKET_NORMAL) or MARKET_NORMAL),
                    after_market_regime=market_regime,
                    context_snapshot_ref=context_ref,
                )
            )
            incident_signatures["market_regime_changed"] = signature
    if (
        str(prev_state.get("system_mode", SYSTEM_NORMAL) or SYSTEM_NORMAL) != system_mode
        or str(prev_state.get("degraded_reason", "") or "") != degraded_reason
        or str(prev_state.get("halt_reason", "") or "") != halt_reason
    ):
        signature = f"system_safety_mode_changed|{system_mode}|{halt_reason}|{degraded_reason}|{market_regime}"
        if str(incident_signatures.get("system_safety_mode_changed", "") or "") != signature:
            incidents.append(
                record_incident(
                    config=config,
                    incident_type="system_safety_mode_changed",
                    severity="error" if system_mode == SYSTEM_HALT else ("warning" if system_mode == SYSTEM_DEGRADED else "info"),
                    component="safety_guard",
                    reason=halt_reason or degraded_reason or "system_normal",
                    action_taken="execution_policy_updated",
                    requires_human_action=system_mode == SYSTEM_HALT,
                    before_system_mode=str(prev_state.get("system_mode", SYSTEM_NORMAL) or SYSTEM_NORMAL),
                    after_system_mode=system_mode,
                    before_market_regime=str(prev_state.get("market_safety_regime", MARKET_NORMAL) or MARKET_NORMAL),
                    after_market_regime=market_regime,
                    context_snapshot_ref=context_ref,
                )
            )
            incident_signatures["system_safety_mode_changed"] = signature
    if incidents:
        last = incidents[-1]
        state["last_incident_level"] = str(last.get("severity", "") or "")
        state["last_incident_type"] = str(last.get("incident_type", "") or "")
    else:
        state["last_incident_level"] = str(prev_state.get("last_incident_level", "") or "")
        state["last_incident_type"] = str(prev_state.get("last_incident_type", "") or "")
    state["incident_signatures"] = incident_signatures
    save_system_safety_state(config=config, state=state)
    return {
        "ok": True,
        "allow_execution": allow_execution,
        "system_mode": system_mode,
        "market_safety_regime": market_regime,
        "manual_halt": bool(overrides.get("manual_halt", False)),
        "manual_reduce_only": bool(overrides.get("manual_reduce_only", False)),
        "effective_reduce_only": effective_reduce_only,
        "effective_turnover_multiplier": turnover_multiplier,
        "panic_reduce_only_ignored": panic_reduce_only_ignored,
        "allow_unfinished_orders_reconcile": allow_unfinished_orders_reconcile,
        "unfinished_orders_reconcile_allowed": unfinished_orders_reconcile_allowed,
        "state": state,
        "incidents": incidents,
        "release_validation": release_validation,
        "account_health": account_health,
        "market": market,
    }
