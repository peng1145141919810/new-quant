from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from ..config_utils import ensure_dir
from ..oms.state_reader import load_latest_oms_actual_state, load_latest_oms_control_feedback
from .admission_engine import apply_admission_replacement
from .contracts import POSITION_LIFECYCLE_FIELDS
from .exposure_engine import build_portfolio_posture
from .lifecycle_engine import build_lifecycle_frame


def _series(frame: pd.DataFrame, column: str | None, default: Any = 0.0) -> pd.Series:
    if column and column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame.index), index=frame.index)


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("portfolio", {}) or {})


def _output_root(config: Dict[str, Any]) -> Path:
    cfg = _cfg(config)
    return ensure_dir(Path(str(cfg.get("output_root", "") or "")).resolve())


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_prev_lifecycle(output_root: Path) -> pd.DataFrame:
    path = output_root / "latest_position_lifecycle.csv"
    if not path.exists():
        return pd.DataFrame(columns=["symbol", "current_state"])
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=["symbol", "current_state"])
    if "symbol" not in df.columns:
        return pd.DataFrame(columns=["symbol", "current_state"])
    return df


def _load_safety_state(config: Dict[str, Any]) -> Dict[str, Any]:
    root = Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve()
    path = root / "system_safety_state.json"
    if not path.exists():
        return {"system_mode": "NORMAL"}
    try:
        return _load_json(path)
    except Exception:
        return {"system_mode": "NORMAL"}


def _load_oms_actual_state(config: Dict[str, Any]) -> Dict[str, Any]:
    if not bool(config.get("oms", {}).get("use_broker_truth_for_v2a_continuity", True)):
        return {}
    try:
        return dict(load_latest_oms_actual_state(config=config) or {})
    except Exception:
        return {}


def _load_control_feedback(config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return dict(load_latest_oms_control_feedback(config=config) or {})
    except Exception:
        return {}


def _current_book_stats(candidate_df: pd.DataFrame, prev_df: pd.DataFrame | None = None) -> Dict[str, Any]:
    prev_df = prev_df if prev_df is not None else pd.DataFrame()
    if prev_df.empty:
        return {
            "current_position_count": int(_series(candidate_df, "is_existing_position", False).fillna(False).astype(bool).sum()) if not candidate_df.empty else 0,
            "current_target_weight_sum": 0.0,
            "weak_existing_count": 0,
        }
    weight_col = "portfolio_weight" if "portfolio_weight" in prev_df.columns else "target_weight" if "target_weight" in prev_df.columns else None
    current_weight_sum = float(pd.to_numeric(_series(prev_df, weight_col, 0.0), errors="coerce").fillna(0.0).sum()) if weight_col else 0.0
    weak_existing_count = 0
    if not candidate_df.empty and "is_existing_position" in candidate_df.columns:
        weak_existing_count = int(
            (
                candidate_df["is_existing_position"].fillna(False).astype(bool)
                & (pd.to_numeric(_series(candidate_df, "tech_allow_entry", False), errors="coerce").fillna(False).astype(bool) == False)
            ).sum()
        )
    return {
        "current_position_count": int(len(prev_df.index)),
        "current_target_weight_sum": current_weight_sum,
        "weak_existing_count": weak_existing_count,
    }


def _upsert_daily(path: Path, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    current = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=POSITION_LIFECYCLE_FIELDS)
    merged = pd.concat([current, frame], ignore_index=True)
    for col in POSITION_LIFECYCLE_FIELDS:
        if col not in merged.columns:
            merged[col] = pd.NA
    merged["date"] = merged["date"].astype(str).str.slice(0, 10)
    merged["symbol"] = merged["symbol"].astype(str)
    merged = merged.drop_duplicates(subset=["date", "symbol"], keep="last")
    merged = merged[POSITION_LIFECYCLE_FIELDS].sort_values(["date", "symbol"])
    merged.to_csv(path, index=False, encoding="utf-8-sig")


def build_portfolio_artifacts(
    config: Dict[str, Any],
    candidate_df: pd.DataFrame,
    prev_df: pd.DataFrame | None,
    market_state: Dict[str, Any],
    portfolio_limits: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = _cfg(config)
    output_root = _output_root(config)
    if not bool(cfg.get("enabled", True)):
        return {"ok": True, "status": "disabled", "frame": candidate_df, "summary": {"status": "disabled"}, "artifacts": {}}
    frame = candidate_df.copy()
    if frame.empty:
        return {"ok": True, "status": "empty", "frame": frame, "summary": {"status": "empty"}, "artifacts": {}}

    if "ts_code" not in frame.columns:
        frame["ts_code"] = frame.get("symbol", frame.get("code", "")).astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str).str.strip().str.upper()

    prev_weight_map: Dict[str, float] = {}
    oms_actual_state = _load_oms_actual_state(config=config)
    oms_positions = list(oms_actual_state.get("positions", []) or [])
    if oms_positions:
        prev_weight_map = {
            str(item.get("symbol", "")).strip().upper(): float(item.get("actual_weight", 0.0) or 0.0)
            for item in oms_positions
            if str(item.get("symbol", "")).strip()
        }
    elif prev_df is not None and not prev_df.empty:
        temp = prev_df.copy()
        if "ts_code" not in temp.columns and "symbol" in temp.columns:
            temp["ts_code"] = temp["symbol"]
        temp["ts_code"] = temp["ts_code"].astype(str).str.strip().str.upper()
        weight_col = "portfolio_weight" if "portfolio_weight" in temp.columns else "target_weight" if "target_weight" in temp.columns else ""
        if weight_col:
            prev_weight_map = {
                str(row["ts_code"]): float(row[weight_col] or 0.0)
                for _, row in temp.iterrows()
            }
    frame["current_weight_ref"] = frame["ts_code"].map(lambda x: float(prev_weight_map.get(str(x), 0.0) or 0.0))

    prev_lifecycle = _load_prev_lifecycle(output_root=output_root)
    prev_state_map = {}
    if oms_positions:
        prev_state_map = {
            str(item.get("symbol", "")).strip().upper(): str(item.get("actual_state", "") or "")
            for item in oms_positions
            if str(item.get("symbol", "")).strip()
        }
    elif not prev_lifecycle.empty and "current_state" in prev_lifecycle.columns:
        prev_state_map = {
            str(row["symbol"]).strip().upper(): str(row["current_state"] or "")
            for _, row in prev_lifecycle.iterrows()
        }
    frame["previous_state"] = frame["ts_code"].map(lambda x: prev_state_map.get(str(x), "hold" if float(prev_weight_map.get(str(x), 0.0) or 0.0) > 0 else "watch"))

    safety_state = _load_safety_state(config=config)
    control_feedback = _load_control_feedback(config=config)
    current_book = _current_book_stats(candidate_df=frame, prev_df=prev_df)
    if oms_positions:
        current_book = {
            "current_position_count": int(sum(float(item.get("actual_weight", 0.0) or 0.0) > 0 for item in oms_positions)),
            "current_target_weight_sum": round(sum(float(item.get("actual_weight", 0.0) or 0.0) for item in oms_positions), 6),
            "weak_existing_count": int(
                (
                    frame["is_existing_position"].fillna(False).astype(bool)
                    & (pd.to_numeric(_series(frame, "tech_allow_entry", False), errors="coerce").fillna(False).astype(bool) == False)
                ).sum()
            ) if "is_existing_position" in frame.columns else 0,
        }
    posture = build_portfolio_posture(
        market_state=market_state,
        safety_state=safety_state,
        current_book=current_book,
        portfolio_limits=portfolio_limits,
        control_feedback=control_feedback,
    )
    lifecycle = build_lifecycle_frame(frame, posture=posture, portfolio_limits=portfolio_limits, cfg=cfg)
    scored, admission_audit = apply_admission_replacement(
        frame=lifecycle,
        posture=posture,
        portfolio_limits=portfolio_limits,
        cfg=cfg,
    )
    if not scored.empty:
        selected_mask = scored["selected_for_target"].fillna(False).astype(bool)
        scored["final_target_weight_v2a"] = scored["proposal_target_weight"].where(selected_mask, 0.0)
        scored["portfolio_weight"] = scored["final_target_weight_v2a"]
    else:
        scored["final_target_weight_v2a"] = 0.0

    latest_lifecycle = output_root / "latest_position_lifecycle.csv"
    daily_lifecycle = output_root / "position_lifecycle_daily.csv"
    posture_path = output_root / "latest_portfolio_posture.json"
    admission_path = output_root / "admission_replacement_audit.json"
    control_summary_path = output_root / "portfolio_control_summary.json"

    lifecycle_frame = scored.copy()
    lifecycle_frame["date"] = datetime.now().strftime("%Y-%m-%d")
    lifecycle_frame["symbol"] = lifecycle_frame["ts_code"]
    for col in POSITION_LIFECYCLE_FIELDS:
        if col not in lifecycle_frame.columns:
            lifecycle_frame[col] = pd.NA
    lifecycle_frame = lifecycle_frame[POSITION_LIFECYCLE_FIELDS].copy()
    lifecycle_frame.to_csv(latest_lifecycle, index=False, encoding="utf-8-sig")
    _upsert_daily(daily_lifecycle, lifecycle_frame)

    control_summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "state_counts": lifecycle_frame["current_state"].astype(str).value_counts().to_dict() if not lifecycle_frame.empty else {},
        "action_counts": lifecycle_frame["recommended_action"].astype(str).value_counts().to_dict() if not lifecycle_frame.empty else {},
        "exposure_usage": {
            "target_cap": float(posture.get("total_exposure_cap", 0.0) or 0.0),
            "proposal_weight_sum": float(pd.to_numeric(_series(scored, "proposal_target_weight", 0.0), errors="coerce").fillna(0.0).sum()) if not scored.empty else 0.0,
            "selected_weight_sum": float(pd.to_numeric(_series(scored, "final_target_weight_v2a", 0.0), errors="coerce").fillna(0.0).sum()) if not scored.empty else 0.0,
            "new_entry_budget": float(posture.get("new_entry_budget", 0.0) or 0.0),
            "add_budget": float(posture.get("add_budget", 0.0) or 0.0),
        },
        "new_entry_count": int(((lifecycle_frame["current_state"].astype(str).isin(["pilot", "build"])) & (~lifecycle_frame["is_existing_position"].fillna(False).astype(bool))).sum()) if not lifecycle_frame.empty else 0,
        "trim_count": int((lifecycle_frame["current_state"].astype(str) == "trim").sum()) if not lifecycle_frame.empty else 0,
        "replacement_count": int(len(list(admission_audit.get("replaced_existing", []) or []))),
        "soft_crowding_penalty_snapshot": {
            "enabled": bool(cfg.get("soft_crowding_penalty_enabled", True)),
            "max_penalty": float(pd.to_numeric(_series(scored, "crowding_penalty", 0.0), errors="coerce").fillna(0.0).max()) if not scored.empty else 0.0,
        },
        "turnover_budget_usage_hint": {
            "rebalance_mode": str(posture.get("rebalance_mode", "") or ""),
            "replacement_aggressiveness": float(posture.get("replacement_aggressiveness", 0.0) or 0.0),
            "defensive_bias": float(posture.get("defensive_bias", 0.0) or 0.0),
        },
    }
    posture_path.write_text(json.dumps(posture, ensure_ascii=False, indent=2), encoding="utf-8")
    admission_path.write_text(json.dumps(admission_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    control_summary_path.write_text(json.dumps(control_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "status": "ok",
        "frame": scored,
        "posture": posture,
        "summary": control_summary,
        "admission_audit": admission_audit,
        "artifacts": {
            "portfolio_posture_path": str(posture_path),
            "position_lifecycle_path": str(latest_lifecycle),
            "position_lifecycle_daily_path": str(daily_lifecycle),
            "admission_replacement_audit_path": str(admission_path),
            "portfolio_control_summary_path": str(control_summary_path),
        },
    }
