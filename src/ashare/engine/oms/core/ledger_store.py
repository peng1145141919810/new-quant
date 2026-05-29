from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from ...sql_store import (
    ensure_schema,
    load_runtime_table,
    replace_runtime_table,
    resolve_sqlite_path,
    sql_store_enabled,
    sqlite_connection,
    upsert_runtime_json_artifact,
)


def _safe_read_csv(path: Path, columns: List[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns].copy()


def load_ledger_frame(path: Path, columns: List[str]) -> pd.DataFrame:
    config = getattr(load_ledger_frame, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    frame = load_runtime_table(conn, path, columns)
                if not frame.empty:
                    return frame
            except Exception:
                pass
    return _safe_read_csv(path, columns=columns)


def write_latest_ledger(path: Path, frame: pd.DataFrame, columns: List[str], key_cols: List[str] | None = None) -> Path:
    out = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[columns].copy()
    if key_cols:
        out = out.drop_duplicates(subset=key_cols, keep="last")
    config = getattr(write_latest_ledger, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            replace_runtime_table(conn, path, out, columns, key_cols=key_cols)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def append_actual_state_daily(path: Path, frame: pd.DataFrame, columns: List[str]) -> Path:
    existing = _safe_read_csv(path, columns=columns)
    config = getattr(append_actual_state_daily, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    sql_existing = load_runtime_table(conn, path, columns)
                if not sql_existing.empty:
                    existing = sql_existing
            except Exception:
                pass
    incoming = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in incoming.columns:
            incoming[col] = pd.NA
    if existing.empty:
        merged = incoming[columns].copy()
    elif incoming.empty:
        merged = existing.copy()
    else:
        merged = pd.concat([existing, incoming[columns]], ignore_index=True)
    merged["date"] = merged["date"].astype(str).str.slice(0, 19)
    merged["symbol"] = merged["symbol"].astype(str)
    merged = merged.drop_duplicates(subset=["date", "symbol"], keep="last")
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            replace_runtime_table(conn, path, merged[columns], columns, key_cols=["date", "symbol"])
    path.parent.mkdir(parents=True, exist_ok=True)
    merged[columns].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def append_frame_rows(path: Path, frame: pd.DataFrame, columns: List[str], dedupe_cols: List[str] | None = None) -> Path:
    existing = _safe_read_csv(path, columns=columns)
    config = getattr(append_frame_rows, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    sql_existing = load_runtime_table(conn, path, columns)
                if not sql_existing.empty:
                    existing = sql_existing
            except Exception:
                pass
    incoming = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in incoming.columns:
            incoming[col] = pd.NA
    if existing.empty:
        merged = incoming[columns].copy()
    elif incoming.empty:
        merged = existing.copy()
    else:
        merged = pd.concat([existing, incoming[columns]], ignore_index=True)
    if dedupe_cols:
        merged = merged.drop_duplicates(subset=dedupe_cols, keep="last")
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            replace_runtime_table(conn, path, merged[columns], columns, key_cols=dedupe_cols)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged[columns].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_json_artifact(path: Path, payload: Dict[str, Any]) -> Path:
    def _json_default(value: Any):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (datetime, pd.Timestamp)):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if value is pd.NA:
            return None
        return str(value)

    config = getattr(write_json_artifact, "_active_config", None)
    clean_payload = json.loads(json.dumps(payload, ensure_ascii=False, default=_json_default))
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            upsert_runtime_json_artifact(conn, path, clean_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean_payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return path


def timestamp_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
