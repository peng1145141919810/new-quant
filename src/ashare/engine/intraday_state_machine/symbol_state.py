from __future__ import annotations

from typing import Any, Dict

import pandas as pd


SYMBOL_STATES = [
    "watch",
    "pilot_entry",
    "build_entry",
    "hold_manage",
    "trim_watch",
    "exit_execute",
    "reconcile_only",
    "freeze",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pick_target_weight(row: Dict[str, Any]) -> float:
    for key in ("final_target_weight_v2a", "proposal_target_weight", "portfolio_weight", "target_weight"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except Exception:
                continue
    return 0.0


def _map_lifecycle_to_symbol_state(source_lifecycle_state: str) -> str:
    state = str(source_lifecycle_state or "").strip().lower()
    return {
        "watch": "watch",
        "pilot": "pilot_entry",
        "build": "build_entry",
        "hold": "hold_manage",
        "trim": "trim_watch",
        "exit": "exit_execute",
    }.get(state, "watch")


def _action_band_for_state(symbol_state: str) -> str:
    mapping = {
        "watch": "observe_only",
        "pilot_entry": "pilot_only",
        "build_entry": "build_allowed",
        "hold_manage": "manage_only",
        "trim_watch": "trim_or_exit",
        "exit_execute": "exit_only",
        "reconcile_only": "reconcile_only",
        "freeze": "freeze",
    }
    return str(mapping.get(str(symbol_state or "").strip(), "observe_only"))


def derive_symbol_state_rows(
    target_frame: pd.DataFrame,
    actual_positions_frame: pd.DataFrame,
    gap_frame: pd.DataFrame,
    intent_state_frame: pd.DataFrame,
    phase_name: str,
    safety_mode: str,
    midday_decision: str,
    release_id: str,
    trade_date: str,
) -> pd.DataFrame:
    target_df = target_frame.copy() if target_frame is not None else pd.DataFrame()
    actual_df = actual_positions_frame.copy() if actual_positions_frame is not None else pd.DataFrame()
    gap_df = gap_frame.copy() if gap_frame is not None else pd.DataFrame()
    intent_df = intent_state_frame.copy() if intent_state_frame is not None else pd.DataFrame()
    symbols = set()
    for frame, column in ((target_df, "symbol"), (target_df, "ts_code"), (actual_df, "symbol"), (gap_df, "symbol"), (intent_df, "stock_code")):
        if not frame.empty and column in frame.columns:
            symbols.update({str(item).strip() for item in frame[column].astype(str).tolist() if str(item).strip() and str(item).strip().lower() != "nan"})
    rows = []
    for symbol in sorted(symbols):
        target_row = {}
        if not target_df.empty:
            if "symbol" in target_df.columns and target_df["symbol"].astype(str).eq(symbol).any():
                target_row = dict(target_df.loc[target_df["symbol"].astype(str).eq(symbol)].iloc[-1].to_dict())
            elif "ts_code" in target_df.columns and target_df["ts_code"].astype(str).eq(symbol).any():
                target_row = dict(target_df.loc[target_df["ts_code"].astype(str).eq(symbol)].iloc[-1].to_dict())
        actual_row = {}
        if not actual_df.empty and "symbol" in actual_df.columns and actual_df["symbol"].astype(str).eq(symbol).any():
            actual_row = dict(actual_df.loc[actual_df["symbol"].astype(str).eq(symbol)].iloc[-1].to_dict())
        gap_row = {}
        if not gap_df.empty and "symbol" in gap_df.columns and gap_df["symbol"].astype(str).eq(symbol).any():
            gap_row = dict(gap_df.loc[gap_df["symbol"].astype(str).eq(symbol)].iloc[-1].to_dict())
        symbol_intents = intent_df.loc[intent_df["stock_code"].astype(str).eq(symbol)].copy() if not intent_df.empty and "stock_code" in intent_df.columns else pd.DataFrame()
        last_intent_state = ""
        last_order_state = ""
        if not symbol_intents.empty:
            symbol_intents = symbol_intents.sort_values(["updated_at", "intent_id"], ascending=[True, True])
            last_intent = dict(symbol_intents.iloc[-1].to_dict())
            last_intent_state = str(last_intent.get("intent_state", "") or "")
            last_order_state = str(last_intent.get("order_status", "") or "")

        source_lifecycle_state = str(
            target_row.get("current_state")
            or target_row.get("desired_state")
            or target_row.get("previous_state")
            or gap_row.get("desired_state")
            or actual_row.get("actual_state")
            or "watch"
        )
        symbol_state = _map_lifecycle_to_symbol_state(source_lifecycle_state)
        target_weight = _pick_target_weight(target_row or gap_row)
        actual_weight = _to_float(actual_row.get("actual_weight", gap_row.get("actual_weight", 0.0)))
        desired_gap = _to_float(gap_row.get("gap_weight", target_weight - actual_weight))
        freeze_reason = ""
        if str(safety_mode or "").upper() == "HALT":
            symbol_state = "freeze"
            freeze_reason = "system_halt"
        elif last_intent_state == "reconcile_only" or bool(actual_row.get("reconcile_required", False)):
            symbol_state = "reconcile_only"
        elif str(midday_decision or "") == "carry_and_reconcile" and abs(desired_gap) < 1e-9 and last_intent_state in {"submitted", "acknowledged", "partial_fill", "stale_pending", "replace_required", "cancel_requested"}:
            symbol_state = "reconcile_only"
        elif str(midday_decision or "") == "abort_new_entries" and actual_weight <= 1e-9 and target_weight > actual_weight:
            symbol_state = "watch"
        elif str(midday_decision or "") == "risk_reduce":
            symbol_state = "trim_watch" if actual_weight > 1e-9 else "watch"
        elif str(safety_mode or "").upper() == "PANIC":
            if actual_weight > 1e-9:
                symbol_state = "trim_watch"
            elif symbol_state in {"pilot_entry", "build_entry"}:
                symbol_state = "freeze"
                freeze_reason = "panic_blocks_new_risk"
        elif last_intent_state in {"stale_pending", "replace_required", "cancel_requested"}:
            symbol_state = "reconcile_only"

        rows.append(
            {
                "trade_date": str(trade_date or ""),
                "release_id": str(release_id or target_row.get("release_id", "") or actual_row.get("release_id", "") or gap_row.get("release_id", "") or ""),
                "stock_code": symbol,
                "symbol_state": symbol_state,
                "source_lifecycle_state": source_lifecycle_state,
                "target_weight": round(float(target_weight), 6),
                "actual_weight": round(float(actual_weight), 6),
                "desired_vs_actual_gap": round(float(desired_gap), 6),
                "current_phase": str(phase_name or ""),
                "current_safety_mode": str(safety_mode or ""),
                "last_intent_state": last_intent_state,
                "last_order_state": last_order_state,
                "action_band": _action_band_for_state(symbol_state),
                "freeze_reason": freeze_reason,
                "updated_at": str(target_row.get("date", "") or actual_row.get("date", "") or target_row.get("price_date", "") or ""),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["stock_code"]).reset_index(drop=True)
    return frame
