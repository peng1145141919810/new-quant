from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd


def _series(frame: pd.DataFrame, column: str, default: Any = False) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame.index), index=frame.index)


def apply_admission_replacement(
    frame: pd.DataFrame,
    posture: Dict[str, Any],
    portfolio_limits: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if frame is None or frame.empty:
        return pd.DataFrame(), {"status": "empty"}
    data = frame.copy()
    max_names = int(portfolio_limits.get("max_names", 20) or 20)
    threshold_base = float(cfg.get("replacement_improvement_threshold", 0.08) or 0.08)
    aggressiveness = float(posture.get("replacement_aggressiveness", 0.5) or 0.5)
    effective_threshold = max(0.02, threshold_base * max(0.45, 1.20 - aggressiveness))
    existing_mask = _series(data, "is_existing_position", False).fillna(False).astype(bool)

    existing_keep = data.loc[existing_mask & data["current_state"].isin(["build", "hold", "trim"])].copy()
    new_pool = data.loc[(~existing_mask) & data["current_state"].isin(["pilot", "build"])].copy()
    exit_pool = data.loc[data["current_state"].isin(["exit"])].copy()

    existing_keep = existing_keep.sort_values(["retention_score", "proposal_target_weight"], ascending=[False, False]).reset_index(drop=True)
    new_pool = new_pool.sort_values(["admission_score", "proposal_target_weight"], ascending=[False, False]).reset_index(drop=True)

    selected_rows: List[pd.Series] = []
    selected_existing: List[pd.Series] = []
    selected_new: List[pd.Series] = []
    denied_new: List[Dict[str, Any]] = []
    replaced_existing: List[Dict[str, Any]] = []

    for _, row in existing_keep.iterrows():
        if len(selected_rows) >= max_names:
            break
        selected_rows.append(row)
        selected_existing.append(row)

    for _, row in new_pool.iterrows():
        if len(selected_rows) < max_names:
            selected_rows.append(row)
            selected_new.append(row)
            continue
        if not bool(cfg.get("admission_replacement_enabled", True)):
            denied_new.append({"symbol": str(row.get("ts_code", row.get("symbol", "")) or ""), "reason": "slot_full_replacement_disabled"})
            continue
        weakest_idx = None
        weakest_score = None
        weakest_row = None
        for idx, selected in enumerate(selected_rows):
            if str(selected.get("current_state", "")) == "build":
                continue
            score = float(selected.get("retention_score", 0.0) or 0.0)
            if weakest_score is None or score < weakest_score:
                weakest_score = score
                weakest_idx = idx
                weakest_row = selected
        improvement = float(row.get("admission_score", 0.0) or 0.0) - float(weakest_score or 0.0)
        if weakest_idx is not None and improvement >= effective_threshold:
            replaced_existing.append(
                {
                    "old_symbol": str(weakest_row.get("ts_code", weakest_row.get("symbol", "")) or ""),
                    "new_symbol": str(row.get("ts_code", row.get("symbol", "")) or ""),
                    "improvement": round(improvement, 6),
                    "threshold": round(effective_threshold, 6),
                }
            )
            selected_rows[weakest_idx] = row
            selected_new.append(row)
        else:
            denied_new.append(
                {
                    "symbol": str(row.get("ts_code", row.get("symbol", "")) or ""),
                    "reason": "replacement_gain_too_small",
                    "improvement": round(improvement, 6),
                    "threshold": round(effective_threshold, 6),
                }
            )

    selected = pd.DataFrame(selected_rows) if selected_rows else pd.DataFrame(columns=data.columns)
    if selected.empty:
        fallback = existing_keep.head(1).copy()
        if fallback.empty:
            fallback = new_pool.head(1).copy()
        selected = fallback

    selected_symbols = set(selected.get("ts_code", pd.Series(dtype=str)).astype(str).tolist()) if not selected.empty else set()
    data["selected_for_target"] = data.get("ts_code", data.get("symbol", "")).astype(str).isin(selected_symbols)
    for item in denied_new:
        symbol = str(item.get("symbol", "") or "")
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == symbol, "drop_reason"] = str(item.get("reason", "") or "")
    for item in replaced_existing:
        old_symbol = str(item.get("old_symbol", "") or "")
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "current_state"] = "exit"
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "recommended_action"] = "sell_exit"
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "position_action_intent"] = "replace_out"
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "proposal_target_weight"] = 0.0
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "target_weight_cap_v2a"] = 0.0
        data.loc[data.get("ts_code", data.get("symbol", "")).astype(str) == old_symbol, "drop_reason"] = "replaced_by_stronger_candidate"

    audit = {
        "status": "ok",
        "selected_count": int(len(selected.index)),
        "selected_existing_count": int(sum(_series(selected, "is_existing_position", False).fillna(False).astype(bool))) if not selected.empty else 0,
        "selected_new_count": int((~_series(selected, "is_existing_position", False).fillna(False).astype(bool)).sum()) if not selected.empty else 0,
        "denied_new": denied_new,
        "replaced_existing": replaced_existing,
        "exit_candidates": exit_pool.get("ts_code", exit_pool.get("symbol", pd.Series(dtype=str))).astype(str).tolist(),
        "replacement_threshold": round(effective_threshold, 6),
    }
    return data, audit
