from __future__ import annotations

import json
from typing import Any, Dict

from .paths import build_oms_paths
from ..sql_store import load_runtime_json_artifact, resolve_sqlite_path, sql_store_enabled, sqlite_connection


def load_latest_oms_actual_state(config: Dict[str, Any]) -> Dict[str, Any]:
    path = build_oms_paths(config)["latest_actual_portfolio_state"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if payload:
                    return payload
            except Exception:
                pass
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_latest_oms_control_feedback(config: Dict[str, Any]) -> Dict[str, Any]:
    path = build_oms_paths(config)["control_feedback_latest"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if payload:
                    return payload
            except Exception:
                pass
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
