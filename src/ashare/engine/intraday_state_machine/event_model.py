from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pandas as pd


EVENT_TYPES = [
    "phase_entered",
    "phase_exited",
    "release_loaded",
    "market_state_refreshed",
    "safety_changed",
    "account_health_stale",
    "account_health_restored",
    "intent_admitted",
    "order_submitted",
    "order_acknowledged",
    "partial_fill_detected",
    "fill_completed",
    "stale_pending_detected",
    "replace_required_detected",
    "cancel_requested",
    "cancel_confirmed",
    "manual_override_applied",
    "midday_plan_published",
    "timing_window_refreshed",
    "timing_buy_ready",
    "timing_sell_ready",
    "timing_frozen",
    "t_armed",
    "t_transition",
    "t_completed",
    "t_frozen",
    "close_reconcile_started",
    "archive_completed",
]


def build_intraday_events(
    *,
    trade_date: str,
    release_id: str,
    namespace: str,
    phase_state: Dict[str, Any],
    safety_state: Dict[str, Any],
    intent_frame: pd.DataFrame,
    symbol_frame: pd.DataFrame,
    now_ts: datetime,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    def add(event_type: str, timestamp: str, stock_code: str = "", payload: Dict[str, Any] | None = None) -> None:
        events.append(
            {
                "event_type": event_type,
                "timestamp": str(timestamp or now_ts.isoformat(timespec="seconds")),
                "trade_date": str(trade_date or ""),
                "release_id": str(release_id or ""),
                "namespace": str(namespace or "main"),
                "stock_code": str(stock_code or ""),
                "payload": dict(payload or {}),
            }
        )

    current_phase = str(phase_state.get("current_phase", "") or "")
    previous_phase = str(phase_state.get("previous_phase", "") or "")
    if current_phase:
        add("phase_entered", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")), payload={"phase": current_phase})
    if previous_phase:
        add("phase_exited", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")), payload={"phase": previous_phase})
    if release_id:
        add("release_loaded", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")), payload={"release_id": release_id})
    add(
        "safety_changed",
        str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")),
        payload={
            "safety_mode": str(phase_state.get("safety_mode", "") or ""),
            "system_mode": str(safety_state.get("system_mode", "") or ""),
            "market_regime": str(safety_state.get("market_safety_regime", "") or ""),
        },
    )
    if str(safety_state.get("account_snapshot_health", "") or "").lower() == "stale":
        add("account_health_stale", now_ts.isoformat(timespec="seconds"))
    else:
        add("account_health_restored", now_ts.isoformat(timespec="seconds"))
    if str(phase_state.get("midday_decision", "") or "").strip():
        add("midday_plan_published", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")), payload={"decision": str(phase_state.get("midday_decision", "") or "")})
    if str(phase_state.get("timing_window", "") or "").strip():
        add(
            "timing_window_refreshed",
            str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")),
            payload={
                "timing_window": str(phase_state.get("timing_window", "") or ""),
                "projected_afternoon_window": str(phase_state.get("projected_afternoon_window", "") or ""),
                "integration_mode": str(phase_state.get("integration_mode", "") or ""),
            },
        )

    if intent_frame is not None and not intent_frame.empty:
        for _, row in intent_frame.iterrows():
            stock_code = str(row.get("stock_code", "") or "")
            ts = str(row.get("updated_at", "") or now_ts.isoformat(timespec="seconds"))
            state = str(row.get("intent_state", "") or "")
            if state == "admitted":
                add("intent_admitted", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or "")})
            elif state == "submitted":
                add("order_submitted", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "order_id": str(row.get("order_id", "") or "")})
            elif state == "acknowledged":
                add("order_acknowledged", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "order_id": str(row.get("order_id", "") or "")})
            elif state == "partial_fill":
                add("partial_fill_detected", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "fill_ratio": row.get("fill_ratio", 0.0)})
            elif state == "filled":
                add("fill_completed", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "fill_ratio": row.get("fill_ratio", 1.0)})
            elif state == "stale_pending":
                add("stale_pending_detected", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "stale_reason": str(row.get("stale_reason", "") or "")})
            elif state == "replace_required":
                add("replace_required_detected", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "parent_intent_id": str(row.get("parent_intent_id", "") or "")})
            elif state == "cancel_requested":
                add("cancel_requested", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "order_id": str(row.get("order_id", "") or "")})
            elif state == "cancelled":
                add("cancel_confirmed", ts, stock_code, {"intent_id": str(row.get("intent_id", "") or ""), "order_id": str(row.get("order_id", "") or "")})

    if symbol_frame is not None and not symbol_frame.empty:
        for _, row in symbol_frame.iterrows():
            stock_code = str(row.get("stock_code", "") or "")
            ts = str(row.get("updated_at", "") or now_ts.isoformat(timespec="seconds"))
            if str(row.get("freeze_reason", "") or "").strip():
                add(
                    "manual_override_applied",
                    ts,
                    stock_code,
                    {
                        "freeze_reason": str(row.get("freeze_reason", "") or ""),
                        "symbol_state": str(row.get("symbol_state", "") or ""),
                    },
                )
            timing_state = str(row.get("timing_state", "") or "").strip()
            if timing_state in {"buy_ready", "dual_ready"}:
                add(
                    "timing_buy_ready",
                    ts,
                    stock_code,
                    {
                        "timing_state": timing_state,
                        "timing_window": str(row.get("timing_window", "") or phase_state.get("timing_window", "") or ""),
                        "buy_timing_score": row.get("buy_timing_score", 0.0),
                    },
                )
            if timing_state in {"sell_ready", "dual_ready"}:
                add(
                    "timing_sell_ready",
                    ts,
                    stock_code,
                    {
                        "timing_state": timing_state,
                        "timing_window": str(row.get("timing_window", "") or phase_state.get("timing_window", "") or ""),
                        "sell_timing_score": row.get("sell_timing_score", 0.0),
                    },
                )
            if timing_state == "timing_frozen":
                add(
                    "timing_frozen",
                    ts,
                    stock_code,
                    {
                        "timing_window": str(row.get("timing_window", "") or phase_state.get("timing_window", "") or ""),
                        "reason": str(row.get("timing_freeze_reason", "") or ""),
                    },
                )
            t_state = str(row.get("t_overlay_state", "") or "").strip()
            if t_state == "t_armed":
                add(
                    "t_armed",
                    ts,
                    stock_code,
                    {
                        "t_allowed_ratio": row.get("t_allowed_ratio", 0.0),
                    },
                )
            if bool(row.get("t_triggered", False)):
                add(
                    "t_transition",
                    ts,
                    stock_code,
                    {
                        "t_overlay_state": t_state,
                        "t_direction": str(row.get("t_direction", "") or ""),
                        "t_leg_done": str(row.get("t_leg_done", "") or ""),
                        "t_trigger_reason": str(row.get("t_trigger_reason", "") or ""),
                        "t_allowed_ratio": row.get("t_allowed_ratio", 0.0),
                    },
                )
            if t_state == "t_completed":
                add(
                    "t_completed",
                    ts,
                    stock_code,
                    {
                        "t_direction": str(row.get("t_direction", "") or ""),
                        "t_rounds_used": row.get("t_rounds_used", 0),
                    },
                )
            if t_state == "t_frozen":
                add(
                    "t_frozen",
                    ts,
                    stock_code,
                    {
                        "t_trigger_reason": str(row.get("t_trigger_reason", "") or ""),
                    },
                )
    if current_phase == "close_reconcile":
        add("close_reconcile_started", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")))
    if current_phase == "postclose_archive":
        add("archive_completed", str(phase_state.get("updated_at", "") or now_ts.isoformat(timespec="seconds")))
    return events
