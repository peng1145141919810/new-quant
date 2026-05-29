from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from ..config_utils import ensure_dir
from ..sql_store import mirror_runtime_dataframe, mirror_runtime_json_artifact, mirror_runtime_jsonl_records


def _intent_table_key_cols(frame: pd.DataFrame) -> List[str] | None:
    for cols in (["symbol", "intent_id"], ["symbol", "rule_id"], ["symbol"]):
        if all(c in frame.columns for c in cols):
            return cols
    return None


def intraday_state_paths(config: Dict[str, Any], trade_date: str) -> Dict[str, Path]:
    cfg = dict(config.get("intraday_state_machine", {}) or {})
    default_root = Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve() / "intraday_state"
    artifact_root = ensure_dir(Path(str(cfg.get("artifact_root", default_root) or default_root)).resolve())
    latest_root = ensure_dir(artifact_root / "latest")
    archive_root = ensure_dir(artifact_root / str(trade_date or "").replace("-", ""))
    return {
        "root": artifact_root,
        "latest": latest_root,
        "archive": archive_root,
        "phase_state_json": latest_root / "intraday_phase_state.json",
        "symbol_state_csv": latest_root / "symbol_execution_state.csv",
        "intent_state_csv": latest_root / "intent_state_daily.csv",
        "event_log_jsonl": latest_root / "intraday_event_log.jsonl",
        "control_summary_json": latest_root / "intraday_control_summary.json",
    }


def _atomic_write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    return _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    text = "\n".join(json.dumps(dict(row or {}), ensure_ascii=False) for row in rows) + "\n"
    return _atomic_write_text(path, text)


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_intraday_artifacts(
    *,
    config: Dict[str, Any],
    trade_date: str,
    phase_state: Dict[str, Any],
    symbol_state_frame: pd.DataFrame,
    intent_state_frame: pd.DataFrame,
    event_rows: list[Dict[str, Any]],
    control_summary: Dict[str, Any],
) -> Dict[str, str]:
    paths = intraday_state_paths(config, trade_date)
    phase_path = _write_json(paths["phase_state_json"], phase_state)
    symbol_path = _write_csv(paths["symbol_state_csv"], symbol_state_frame)
    intent_path = _write_csv(paths["intent_state_csv"], intent_state_frame)
    event_path = _write_jsonl(paths["event_log_jsonl"], event_rows)
    control_path = _write_json(paths["control_summary_json"], control_summary)

    archive_phase = _write_json(paths["archive"] / phase_path.name, phase_state)
    archive_symbol = _write_csv(paths["archive"] / symbol_path.name, symbol_state_frame)
    archive_intent = _write_csv(paths["archive"] / intent_path.name, intent_state_frame)
    archive_event = _write_jsonl(paths["archive"] / event_path.name, event_rows)
    archive_control = _write_json(paths["archive"] / control_path.name, control_summary)

    manifest = {
        "generated_at": str(control_summary.get("generated_at", "") or ""),
        "trade_date": str(trade_date or ""),
        "latest_root": str(paths["latest"]),
        "archive_root": str(paths["archive"]),
        "phase_state_path": str(phase_path),
        "symbol_state_path": str(symbol_path),
        "intent_state_path": str(intent_path),
        "event_log_path": str(event_path),
        "control_summary_path": str(control_path),
        "archive_phase_state_path": str(archive_phase),
        "archive_symbol_state_path": str(archive_symbol),
        "archive_intent_state_path": str(archive_intent),
        "archive_event_log_path": str(archive_event),
        "archive_control_summary_path": str(archive_control),
    }
    latest_manifest = paths["latest"] / "intraday_state_manifest.json"
    _write_json(latest_manifest, manifest)
    _write_json(paths["archive"] / "intraday_state_manifest.json", manifest)

    sk = ["symbol"] if "symbol" in symbol_state_frame.columns else None
    mirror_runtime_json_artifact(config, paths["phase_state_json"], phase_state)
    mirror_runtime_dataframe(config, paths["symbol_state_csv"], symbol_state_frame, key_cols=sk)
    mirror_runtime_dataframe(config, paths["intent_state_csv"], intent_state_frame, key_cols=_intent_table_key_cols(intent_state_frame))
    mirror_runtime_jsonl_records(config, paths["event_log_jsonl"], event_rows)
    mirror_runtime_json_artifact(config, paths["control_summary_json"], control_summary)
    mirror_runtime_json_artifact(config, latest_manifest, manifest)

    return manifest
