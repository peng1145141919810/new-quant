from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .flow_features import build_intraday_feature_frame
from .t_overlay import apply_t_overlay
from .timing_rules import apply_timing_rules
from .timing_scores import compute_timing_scores
from .timing_windows import projected_window, resolve_timing_window


def _latest_symbol_state_path(config: Dict[str, Any]) -> Path:
    intraday_cfg = dict(config.get("intraday_state_machine", {}) or {})
    default_root = Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve() / "intraday_state"
    root = Path(str(intraday_cfg.get("artifact_root", default_root) or default_root)).resolve()
    return root / "latest" / "symbol_execution_state.csv"


def _read_previous_symbol_frame(config: Dict[str, Any], trade_date: str) -> pd.DataFrame:
    path = _latest_symbol_state_path(config)
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    return frame.loc[frame["trade_date"].astype(str).eq(str(trade_date or ""))].copy()


def _merge_on_stock_code(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    if right is None or right.empty:
        return left.copy()
    payload = right.copy()
    if "stock_code" not in payload.columns:
        return left.copy()
    return left.merge(payload, how="left", on="stock_code")


def build_timing_overlay_payload(
    *,
    config: Dict[str, Any],
    trade_date: str,
    now_dt: Any,
    current_phase: str,
    symbol_state_frame: pd.DataFrame,
    target_frame: pd.DataFrame,
    actual_positions_frame: pd.DataFrame,
    market_state: Dict[str, Any],
    safety_mode: str,
) -> Dict[str, Any]:
    if symbol_state_frame is None or symbol_state_frame.empty:
        return {
            "symbol_state_frame": pd.DataFrame(),
            "timing_summary": {
                "timing_window": "out_of_session",
                "timing_enabled_symbols": 0,
                "t_eligible_symbols": 0,
                "t_triggered_symbols": 0,
                "buy_window_open_count": 0,
                "sell_window_open_count": 0,
                "timing_frozen_count": 0,
                "t_completed_count": 0,
            },
        }

    current_window = resolve_timing_window(config=config, now_dt=now_dt, phase_name=current_phase)
    afternoon_projection = projected_window(config, "afternoon_primary_window")
    previous_frame = _read_previous_symbol_frame(config=config, trade_date=trade_date)

    feature_frame = build_intraday_feature_frame(
        config=config,
        trade_date=trade_date,
        target_frame=target_frame,
        actual_positions_frame=actual_positions_frame,
        symbol_state_frame=symbol_state_frame,
        market_state=market_state,
    )
    score_frame = compute_timing_scores(feature_frame=feature_frame, market_state=market_state, safety_mode=safety_mode)
    enriched = _merge_on_stock_code(symbol_state_frame.copy(), feature_frame)
    enriched = _merge_on_stock_code(enriched, score_frame)
    timing_state_frame = apply_timing_rules(frame=enriched, current_window=current_window, safety_mode=safety_mode, config=config)
    enriched = _merge_on_stock_code(enriched, timing_state_frame)
    t_overlay_frame = apply_t_overlay(
        frame=enriched,
        previous_frame=previous_frame,
        current_phase=current_phase,
        current_window=current_window,
        config=config,
        safety_mode=safety_mode,
        trade_date=trade_date,
    )
    enriched = _merge_on_stock_code(enriched, t_overlay_frame)
    if "stock_code" in enriched.columns:
        enriched = enriched.sort_values(["stock_code"]).reset_index(drop=True)

    timing_enabled_symbols = int(pd.to_numeric(enriched.get("timing_enabled", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(bool).sum())
    t_eligible_symbols = int(pd.to_numeric(enriched.get("t_eligible", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(bool).sum())
    t_triggered_symbols = int(pd.to_numeric(enriched.get("t_triggered", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(bool).sum())
    buy_window_open_count = int(pd.to_numeric(enriched.get("buy_window_open", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(bool).sum())
    sell_window_open_count = int(pd.to_numeric(enriched.get("sell_window_open", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(bool).sum())
    timing_frozen_count = int(enriched.get("timing_state", pd.Series(dtype=object)).astype(str).eq("timing_frozen").sum())
    t_completed_count = int(enriched.get("t_overlay_state", pd.Series(dtype=object)).astype(str).eq("t_completed").sum())
    buy_ready_count = int(enriched.get("timing_state", pd.Series(dtype=object)).astype(str).isin(["buy_ready", "dual_ready"]).sum())
    sell_ready_count = int(enriched.get("timing_state", pd.Series(dtype=object)).astype(str).isin(["sell_ready", "dual_ready"]).sum())
    afternoon_second_leg_candidates = int(
        enriched.get("t_overlay_state", pd.Series(dtype=object)).astype(str).isin(
            ["t_sell_leg_done_wait_buyback", "t_buy_leg_done_wait_sellback"]
        ).sum()
    )

    timing_summary = {
        "timing_window": str(current_window.get("name", "") or ""),
        "timing_window_active": bool(current_window.get("active", False)),
        "timing_window_projection": str(afternoon_projection.get("name", "") or ""),
        "timing_enabled_symbols": timing_enabled_symbols,
        "t_eligible_symbols": t_eligible_symbols,
        "t_triggered_symbols": t_triggered_symbols,
        "buy_window_open_count": buy_window_open_count,
        "sell_window_open_count": sell_window_open_count,
        "timing_frozen_count": timing_frozen_count,
        "t_completed_count": t_completed_count,
        "buy_ready_count": buy_ready_count,
        "sell_ready_count": sell_ready_count,
        "afternoon_second_leg_candidates": afternoon_second_leg_candidates,
        "feature_quality_counts": {
            str(k): int(v)
            for k, v in enriched.get("feature_quality_tier", pd.Series(dtype=object)).astype(str).value_counts().to_dict().items()
        },
        "overlay_recommendation": {
            "timing_window": str(current_window.get("name", "") or ""),
            "projected_afternoon_window": str(afternoon_projection.get("name", "") or ""),
            "timing_layer_active": bool(timing_enabled_symbols > 0),
            "buy_ready_count": buy_ready_count,
            "sell_ready_count": sell_ready_count,
            "afternoon_second_leg_candidates_count": afternoon_second_leg_candidates,
            "t_triggered_count": t_triggered_symbols,
            "block_new_t": bool(str(safety_mode or "").upper() == "HALT"),
            "panic_degrade_only": bool(str(safety_mode or "").upper() == "PANIC"),
        },
    }
    return {
        "symbol_state_frame": enriched,
        "timing_summary": timing_summary,
        "current_window": current_window,
        "afternoon_projection": afternoon_projection,
    }
