from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from ..config_utils import ensure_dir
from ..logging_utils import log_line
from .contracts import TECH_CONFIRMATION_FIELDS
from .core.feature_builder import build_candidate_features, _normalize_ts_code
from .core.scorer import score_technical_frame
from .policy.policy import summarize_technical_frame


def _tech_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("technical_confirmation", {}) or {})


def _output_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("technical_confirmation_root", "") or "")).resolve())


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _config_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(str(_tech_cfg(config).get("config_path", "") or "")).resolve()
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def build_technical_confirmation_artifacts(
    config: Dict[str, Any],
    candidate_df: pd.DataFrame,
    prev_df: pd.DataFrame | None = None,
    market_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    tech_cfg = _tech_cfg(config)
    output_root = _output_root(config)
    summary_path = output_root / "technical_confirmation_summary.json"
    latest_path = output_root / "latest_technical_confirmation.csv"
    archive_path = output_root / "technical_confirmation_daily.csv"
    if not bool(tech_cfg.get("enabled", True)):
        empty = pd.DataFrame(columns=TECH_CONFIRMATION_FIELDS)
        empty.to_csv(latest_path, index=False, encoding="utf-8-sig")
        empty.to_csv(archive_path, index=False, encoding="utf-8-sig")
        summary = {"status": "disabled", "rows": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "status": "disabled", "frame": empty, "summary": summary, "latest_path": str(latest_path)}
    if candidate_df is None or candidate_df.empty:
        empty = pd.DataFrame(columns=TECH_CONFIRMATION_FIELDS)
        empty.to_csv(latest_path, index=False, encoding="utf-8-sig")
        empty.to_csv(archive_path, index=False, encoding="utf-8-sig")
        summary = {"status": "empty", "rows": 0}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "status": "empty", "frame": empty, "summary": summary, "latest_path": str(latest_path)}
    payload = _config_payload(config)
    strictness = float((market_state or {}).get("entry_strictness", 0.5) or 0.5)
    enriched_dir = Path(str(config.get("market_pipeline", {}).get("enriched_dir", "") or "")).resolve()
    prev_symbols = []
    if prev_df is not None and not prev_df.empty:
        for field in ["ts_code", "code"]:
            if field in prev_df.columns:
                prev_symbols.extend(prev_df[field].dropna().astype(str).tolist())
    feature_df = build_candidate_features(candidate_df=candidate_df, prev_symbols=prev_symbols, enriched_dir=enriched_dir)
    scored = score_technical_frame(feature_df=feature_df, strictness=strictness, config_payload=payload)
    if not scored.empty:
        for col in TECH_CONFIRMATION_FIELDS:
            if col not in scored.columns:
                scored[col] = pd.NA
        scored = scored[TECH_CONFIRMATION_FIELDS].copy()
    scored.to_csv(latest_path, index=False, encoding="utf-8-sig")
    scored.to_csv(archive_path, index=False, encoding="utf-8-sig")
    summary = summarize_technical_frame(scored, strictness=strictness)
    summary.update(
        {
            "status": "ok",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "latest_path": str(latest_path),
            "archive_path": str(archive_path),
        }
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(
        config,
        (
            "Technical Confirmation: 完成 "
            f"rows={summary.get('rows', 0)} allow={summary.get('allow_count', 0)} "
            f"wait={summary.get('wait_count', 0)} strictness={strictness:.2f}"
        ),
    )
    return {
        "ok": True,
        "status": "ok",
        "frame": scored,
        "summary": summary,
        "latest_path": str(latest_path),
        "summary_path": str(summary_path),
    }
