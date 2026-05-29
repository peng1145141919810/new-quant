from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from ..config_utils import ensure_dir


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trade_date_match(payload: Dict[str, Any], trade_date: str) -> bool:
    expected = str(trade_date or "").strip()
    if not expected:
        return True
    actual = str(payload.get("trade_date", "") or "").strip()
    return bool(actual) and actual == expected


def _artifact_path(manifest: Dict[str, Any], key: str, fallback: Path) -> Path:
    raw = str(dict(manifest.get("artifacts", {}) or {}).get(key, "") or "").strip()
    return Path(raw).resolve() if raw else fallback


def tactics_root(config: Dict[str, Any]) -> Path:
    data_root = Path(str(config.get("paths", {}).get("data_root", "") or _repo_root() / "data")).resolve()
    raw = dict(config.get("intraday_tactics", {}) or {}).get("artifact_root", "")
    if str(raw).strip():
        return ensure_dir(Path(str(raw)).resolve())
    return ensure_dir(data_root / "trade_clock" / "intraday_tactics")


def load_tactical_context(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    data_root = Path(str(config.get("paths", {}).get("data_root", "") or _repo_root() / "data")).resolve()
    tc_root = Path(str(config.get("paths", {}).get("trade_clock_root", "") or data_root / "trade_clock")).resolve()
    oms_root = Path(str(config.get("paths", {}).get("oms_output_root", "") or data_root / "live_execution_bridge" / "oms_v1")).resolve()
    proxy_root = Path(str(config.get("paths", {}).get("intraday_proxy_root", "") or tc_root / "intraday_proxy")).resolve()
    intraday_state = Path(str(config.get("intraday_state_machine", {}).get("artifact_root", "") or tc_root / "intraday_state")).resolve()

    release = _read_json(data_root / "trade_release_v1" / "latest_release.json")
    if release and not _trade_date_match(release, trade_date):
        release = {}
    release_id = str(release.get("release_id", "") or "")
    manifest = _read_json(data_root / "trade_release_v1" / "releases" / release_id / "release_manifest.json") if release_id else {}
    if manifest and not _trade_date_match(manifest, trade_date):
        manifest = {}
        release = {}
        release_id = ""

    portfolio_root = Path(str(config.get("paths", {}).get("portfolio_output_root", "") or data_root / "portfolio_recommendation_v6")).resolve()
    target_csv = _artifact_path(manifest, "target_positions_path", portfolio_root / "target_positions.csv") if manifest else (portfolio_root / "target_positions.csv")
    lifecycle_csv = _artifact_path(manifest, "position_lifecycle_path", portfolio_root / "portfolio" / "latest_position_lifecycle.csv") if manifest else (portfolio_root / "portfolio" / "latest_position_lifecycle.csv")
    portfolio_summary_path = _artifact_path(manifest, "portfolio_summary_path", portfolio_root / "portfolio_recommendation.json") if manifest else (portfolio_root / "portfolio_recommendation.json")

    symbol_csv = intraday_state / "latest" / "symbol_execution_state.csv"
    control_json = intraday_state / "latest" / "intraday_control_summary.json"
    phase_json = intraday_state / "latest" / "intraday_phase_state.json"

    clock_snap = _read_json(tc_root / "clock_account_snapshot.json")
    truth = _read_json(proxy_root / "latest" / "account_truth_snapshot.json")
    proxy_manifest = _read_json(proxy_root / "latest" / "intraday_proxy_manifest.json")
    phase_state = _read_json(phase_json) if phase_json.exists() else {}
    if phase_state and not _trade_date_match(phase_state, trade_date):
        phase_state = {}
    if truth and not _trade_date_match(truth, trade_date):
        truth = {}
    if proxy_manifest and not _trade_date_match(proxy_manifest, trade_date):
        proxy_manifest = {}

    actual = _read_json(oms_root / "snapshots" / "latest_actual_portfolio_state.json")
    gap_csv = oms_root / "snapshots" / "desired_vs_actual_gap.csv"

    symbol_frame = pd.read_csv(symbol_csv) if symbol_csv.exists() else pd.DataFrame()
    gap_frame = pd.read_csv(gap_csv) if gap_csv.exists() else pd.DataFrame()
    lifecycle_df = pd.read_csv(lifecycle_csv) if lifecycle_csv.exists() else pd.DataFrame()

    market_state = _read_json(data_root / "market_state" / "latest_market_state.json")

    return {
        "trade_date": str(trade_date or ""),
        "release": release,
        "release_id": release_id,
        "manifest": manifest,
        "target_positions_path": str(target_csv),
        "lifecycle_path": str(lifecycle_csv),
        "portfolio_summary": _read_json(portfolio_summary_path),
        "portfolio_summary_path": str(portfolio_summary_path),
        "symbol_frame": symbol_frame,
        "gap_frame": gap_frame,
        "lifecycle_df": lifecycle_df,
        "control_summary": _read_json(control_json) if control_json.exists() else {},
        "phase_state": phase_state,
        "clock_account_snapshot": clock_snap,
        "account_truth": truth,
        "proxy_manifest": proxy_manifest,
        "actual_portfolio": actual,
        "market_state": market_state,
        "paths": {
            "tactics_root": str(tactics_root(config)),
            "oms_root": str(oms_root),
            "intraday_state": str(intraday_state),
        },
    }
