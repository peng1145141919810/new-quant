from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

from live_execution_bridge.models import OrderIntent
from ...sql_store import (
    append_runtime_jsonl_record,
    ensure_schema,
    load_runtime_json_artifact,
    resolve_sqlite_path,
    sql_store_enabled,
    sqlite_connection,
    upsert_runtime_json_artifact,
)


VALID_FORCE_STATES = {"watch", "pilot", "build", "hold", "trim", "exit"}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_symbol_list(items: Iterable[Any]) -> List[str]:
    values = sorted({str(item).strip().upper() for item in list(items or []) if str(item).strip()})
    return values


def _normalize_string_list(items: Iterable[Any]) -> List[str]:
    values = sorted({str(item).strip() for item in list(items or []) if str(item).strip()})
    return values


def _normalize_force_actual_state(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    for key, value in dict(raw or {}).items():
        symbol = str(key).strip().upper()
        if not symbol:
            continue
        if isinstance(value, dict):
            state = str(value.get("state", "") or "").strip().lower()
            reason = str(value.get("reason", "operator_force_state") or "operator_force_state")
        else:
            state = str(value or "").strip().lower()
            reason = "operator_force_state"
        if state not in VALID_FORCE_STATES:
            continue
        normalized[symbol] = {"state": state, "reason": reason}
    return normalized


def _normalize_ignore_orders(items: Iterable[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in list(items or []):
        if isinstance(item, dict):
            row = {
                "order_id": str(item.get("order_id", "") or "").strip(),
                "cl_ord_id": str(item.get("cl_ord_id", "") or "").strip(),
                "symbol": str(item.get("symbol", "") or "").strip().upper(),
                "reason": str(item.get("reason", "operator_confirmed_dead") or "operator_confirmed_dead"),
            }
        else:
            row = {
                "order_id": str(item or "").strip(),
                "cl_ord_id": "",
                "symbol": "",
                "reason": "operator_confirmed_dead",
            }
        if row["order_id"] or row["cl_ord_id"] or row["symbol"]:
            rows.append(row)
    rows.sort(key=lambda x: (x["order_id"], x["cl_ord_id"], x["symbol"]))
    return rows


def _default_overrides() -> Dict[str, Any]:
    return {
        "version": 2,
        "updated_at": "",
        "operator_id": "",
        "notes": "",
        "intent_controls": {
            "force_close_intents": [],
            "force_expire_intents": [],
            "operator_cancel_intents": [],
            "freeze_intent_continuation": [],
        },
        "symbol_controls": {
            "freeze_new_entry_symbols": [],
            "freeze_build_symbols": [],
            "force_actual_state": {},
            "reconcile_required_symbols": [],
        },
        "session_controls": {
            "ignore_stale_unfinished_orders": [],
            "expire_orders": [],
            "force_resync": False,
            "force_reconcile_only": False,
            "rebuild_actual_state": False,
        },
    }


def _upgrade_legacy(payload: Dict[str, Any]) -> Dict[str, Any]:
    upgraded = _default_overrides()
    upgraded.update({k: v for k, v in dict(payload or {}).items() if k in {"version", "updated_at", "operator_id", "notes"}})
    intent_controls = dict(upgraded["intent_controls"])
    symbol_controls = dict(upgraded["symbol_controls"])
    session_controls = dict(upgraded["session_controls"])

    if "intent_controls" in payload:
        intent_controls.update(dict(payload.get("intent_controls", {}) or {}))
    if "symbol_controls" in payload:
        symbol_controls.update(dict(payload.get("symbol_controls", {}) or {}))
    if "session_controls" in payload:
        session_controls.update(dict(payload.get("session_controls", {}) or {}))

    legacy_frozen = list(payload.get("frozen_symbols", []) or [])
    if legacy_frozen:
        symbol_controls["freeze_new_entry_symbols"] = list(symbol_controls.get("freeze_new_entry_symbols", []) or []) + legacy_frozen
        symbol_controls["freeze_build_symbols"] = list(symbol_controls.get("freeze_build_symbols", []) or []) + legacy_frozen
    legacy_force_close = list(payload.get("force_close_intents", []) or [])
    if legacy_force_close:
        intent_controls["force_close_intents"] = list(intent_controls.get("force_close_intents", []) or []) + legacy_force_close
    legacy_expire_orders = list(payload.get("expire_orders", []) or [])
    if legacy_expire_orders:
        session_controls["expire_orders"] = list(session_controls.get("expire_orders", []) or []) + legacy_expire_orders
    if bool(payload.get("force_resync", False)):
        session_controls["force_resync"] = True

    upgraded["intent_controls"] = {
        "force_close_intents": _normalize_string_list(intent_controls.get("force_close_intents", [])),
        "force_expire_intents": _normalize_string_list(intent_controls.get("force_expire_intents", [])),
        "operator_cancel_intents": _normalize_string_list(intent_controls.get("operator_cancel_intents", [])),
        "freeze_intent_continuation": _normalize_symbol_list(intent_controls.get("freeze_intent_continuation", [])),
    }
    upgraded["symbol_controls"] = {
        "freeze_new_entry_symbols": _normalize_symbol_list(symbol_controls.get("freeze_new_entry_symbols", [])),
        "freeze_build_symbols": _normalize_symbol_list(symbol_controls.get("freeze_build_symbols", [])),
        "force_actual_state": _normalize_force_actual_state(symbol_controls.get("force_actual_state", {})),
        "reconcile_required_symbols": _normalize_symbol_list(symbol_controls.get("reconcile_required_symbols", [])),
    }
    upgraded["session_controls"] = {
        "ignore_stale_unfinished_orders": _normalize_ignore_orders(session_controls.get("ignore_stale_unfinished_orders", [])),
        "expire_orders": _normalize_string_list(session_controls.get("expire_orders", [])),
        "force_resync": bool(session_controls.get("force_resync", False)),
        "force_reconcile_only": bool(session_controls.get("force_reconcile_only", False)),
        "rebuild_actual_state": bool(session_controls.get("rebuild_actual_state", False)),
    }
    if not str(upgraded.get("updated_at", "") or "").strip():
        upgraded["updated_at"] = _now_text()
    return upgraded


def ensure_manual_overrides(path: Path) -> Dict[str, Any]:
    config = getattr(ensure_manual_overrides, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if isinstance(payload, dict) and payload:
                    normalized = _upgrade_legacy(dict(payload or {}))
                    with sqlite_connection(db_path) as conn:
                        ensure_schema(conn)
                        upsert_runtime_json_artifact(conn, path, normalized)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
                    return normalized
            except Exception:
                pass
    if not path.exists():
        payload = _default_overrides()
        payload["updated_at"] = _now_text()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if isinstance(config, dict) and sql_store_enabled(config):
            with sqlite_connection(resolve_sqlite_path(config)) as conn:
                ensure_schema(conn)
                upsert_runtime_json_artifact(conn, path, payload)
        return payload
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = _default_overrides()
    normalized = _upgrade_legacy(dict(payload or {}))
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            upsert_runtime_json_artifact(conn, path, normalized)
    return normalized


def build_manual_override_summary(overrides: Dict[str, Any]) -> Dict[str, Any]:
    intent_controls = dict(overrides.get("intent_controls", {}) or {})
    symbol_controls = dict(overrides.get("symbol_controls", {}) or {})
    session_controls = dict(overrides.get("session_controls", {}) or {})
    return {
        "active_intent_controls": sum(len(list(intent_controls.get(key, []) or [])) for key in ["force_close_intents", "force_expire_intents", "operator_cancel_intents", "freeze_intent_continuation"]),
        "active_symbol_controls": len(list(symbol_controls.get("freeze_new_entry_symbols", []) or []))
        + len(list(symbol_controls.get("freeze_build_symbols", []) or []))
        + len(list(symbol_controls.get("reconcile_required_symbols", []) or []))
        + len(dict(symbol_controls.get("force_actual_state", {}) or {})),
        "active_session_controls": len(list(session_controls.get("ignore_stale_unfinished_orders", []) or []))
        + len(list(session_controls.get("expire_orders", []) or []))
        + int(bool(session_controls.get("force_resync", False)))
        + int(bool(session_controls.get("force_reconcile_only", False)))
        + int(bool(session_controls.get("rebuild_actual_state", False))),
    }


def _override_hash(overrides: Dict[str, Any]) -> str:
    text = json.dumps(overrides, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def record_manual_intervention_state(
    latest_state_path: Path,
    history_path: Path,
    overrides: Dict[str, Any],
    applied_summary: Dict[str, Any],
) -> Dict[str, Any]:
    config = getattr(record_manual_intervention_state, "_active_config", None)
    summary = build_manual_override_summary(overrides=overrides)
    payload = {
        "generated_at": _now_text(),
        "override_hash": _override_hash(overrides=overrides),
        "operator_id": str(overrides.get("operator_id", "") or ""),
        "notes": str(overrides.get("notes", "") or ""),
        "summary": summary,
        "applied_summary": dict(applied_summary or {}),
        "active_overrides": overrides,
    }
    latest_state_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    prior_hash = ""
    if latest_state_path.exists():
        try:
            prior_hash = str(json.loads(latest_state_path.read_text(encoding="utf-8")).get("override_hash", "") or "")
        except Exception:
            prior_hash = ""
    latest_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            upsert_runtime_json_artifact(conn, latest_state_path, payload)
    if payload["override_hash"] != prior_hash or any(int(v or 0) > 0 for v in summary.values()) or any(int(v or 0) > 0 for v in dict(applied_summary or {}).values() if isinstance(v, (int, float, bool))):
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if isinstance(config, dict) and sql_store_enabled(config):
            with sqlite_connection(resolve_sqlite_path(config)) as conn:
                ensure_schema(conn)
                append_runtime_jsonl_record(conn, history_path, payload, record_id=payload["override_hash"] + "_" + payload["generated_at"])
    return payload


def filter_unfinished_orders_by_override(
    unfinished_orders: Iterable[Dict[str, Any]],
    overrides: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows = [dict(item) for item in list(unfinished_orders or [])]
    ignore_items = list(dict(overrides.get("session_controls", {}) or {}).get("ignore_stale_unfinished_orders", []) or [])
    ignore_order_ids = {str(item.get("order_id", "") or "").strip() for item in ignore_items if str(item.get("order_id", "") or "").strip()}
    ignore_cl_ids = {str(item.get("cl_ord_id", "") or "").strip() for item in ignore_items if str(item.get("cl_ord_id", "") or "").strip()}
    ignore_symbols = {str(item.get("symbol", "") or "").strip().upper() for item in ignore_items if str(item.get("symbol", "") or "").strip()}
    kept: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    for row in rows:
        order_id = str(row.get("order_id", "") or "").strip()
        cl_ord_id = str(row.get("cl_ord_id", "") or "").strip()
        symbol = str(row.get("symbol", "") or "").strip().upper()
        if order_id in ignore_order_ids or cl_ord_id in ignore_cl_ids or symbol in ignore_symbols:
            ignored.append({**row, "ignored_reason": "operator_confirmed_dead"})
            continue
        kept.append(row)
    return kept, ignored


def apply_manual_overrides_to_actual_state(
    actual_state_frame: pd.DataFrame,
    overrides: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    frame = actual_state_frame.copy() if actual_state_frame is not None else pd.DataFrame()
    if frame.empty:
        return frame, []
    symbol_controls = dict(overrides.get("symbol_controls", {}) or {})
    force_states = _normalize_force_actual_state(symbol_controls.get("force_actual_state", {}))
    reconcile_required = set(_normalize_symbol_list(symbol_controls.get("reconcile_required_symbols", [])))
    audit_rows: List[Dict[str, Any]] = []
    for idx, row in frame.iterrows():
        symbol = str(row.get("symbol", "") or "").strip().upper()
        if not symbol:
            continue
        if "raw_actual_state" not in frame.columns:
            frame.loc[idx, "raw_actual_state"] = str(row.get("actual_state", "") or "")
        if symbol in reconcile_required:
            frame.loc[idx, "reconcile_required"] = True
            frame.loc[idx, "manual_override_reason"] = str(frame.loc[idx, "manual_override_reason"] or "manual_reconcile_required")
            audit_rows.append({"symbol": symbol, "type": "reconcile_required", "reason": "manual_reconcile_required"})
        override = force_states.get(symbol)
        if not override:
            continue
        frame.loc[idx, "raw_actual_state"] = str(row.get("actual_state", "") or row.get("raw_actual_state", "") or "")
        frame.loc[idx, "actual_state_override"] = str(override.get("state", "") or "")
        frame.loc[idx, "actual_state"] = str(override.get("state", "") or "")
        frame.loc[idx, "actual_state_source"] = "operator_override"
        frame.loc[idx, "manual_override_reason"] = str(override.get("reason", "operator_force_state") or "operator_force_state")
        frame.loc[idx, "reconcile_required"] = True
        audit_rows.append({"symbol": symbol, "type": "force_actual_state", "state": str(override.get("state", "") or ""), "reason": str(override.get("reason", "") or "operator_force_state")})
    return frame, audit_rows


def apply_manual_overrides_to_orders(
    orders: Iterable[OrderIntent],
    overrides: Dict[str, Any],
) -> Tuple[List[OrderIntent], List[Dict[str, Any]]]:
    symbol_controls = dict(overrides.get("symbol_controls", {}) or {})
    freeze_new = set(_normalize_symbol_list(symbol_controls.get("freeze_new_entry_symbols", [])))
    freeze_build = set(_normalize_symbol_list(symbol_controls.get("freeze_build_symbols", [])))
    accepted: List[OrderIntent] = []
    blocked: List[Dict[str, Any]] = []
    for order in list(orders or []):
        symbol = str(order.symbol or "").strip().upper()
        reason = ""
        side = str(order.side).upper()
        if symbol in freeze_new and side == "BUY":
            reason = "manual_symbol_freeze_new_entry"
        elif symbol in freeze_build and side == "BUY":
            reason = "manual_symbol_freeze_build"
        if reason:
            blocked.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "reason": reason,
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                }
            )
            continue
        accepted.append(order)
    return accepted, blocked
