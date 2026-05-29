from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

import pandas as pd


INTENT_STATES = [
    "planned",
    "admitted",
    "submitted",
    "acknowledged",
    "partial_fill",
    "filled",
    "stale_pending",
    "replace_required",
    "cancel_requested",
    "cancelled",
    "reconcile_only",
    "aborted",
]


def _parse_ts(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "nan":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def derive_intent_state_rows(
    intent_frame: pd.DataFrame,
    order_frame: pd.DataFrame,
    fill_frame: pd.DataFrame,
    cancel_replace_audit: Dict[str, Any],
    continuity_report: Dict[str, Any],
    namespace: str,
    trade_date: str,
    release_id: str,
    force_reconcile_only: bool = False,
    stale_minutes: int = 20,
    now_ts: datetime | None = None,
) -> pd.DataFrame:
    intents = intent_frame.copy() if intent_frame is not None else pd.DataFrame()
    orders = order_frame.copy() if order_frame is not None else pd.DataFrame()
    fills = fill_frame.copy() if fill_frame is not None else pd.DataFrame()
    cancel_requests = list(cancel_replace_audit.get("cancel_requests", []) or [])
    replacement_links = list(cancel_replace_audit.get("replacement_links", []) or [])
    continuity_rows = list(continuity_report.get("rows", []) or [])
    continuity_map = {str(item.get("intent_id", "") or ""): dict(item or {}) for item in continuity_rows if str(item.get("intent_id", "") or "").strip()}
    cancel_intent_ids = {str(item.get("intent_id", "") or "") for item in cancel_requests if str(item.get("intent_id", "") or "").strip()}
    replace_old_ids = {str(item.get("old_intent_id", "") or "") for item in replacement_links if str(item.get("old_intent_id", "") or "").strip()}
    now_value = now_ts or datetime.now()
    if intents.empty:
        return pd.DataFrame(
            columns=[
                "trade_date",
                "release_id",
                "namespace",
                "intent_id",
                "stock_code",
                "intent_state",
                "order_id",
                "fill_ratio",
                "is_replace_lineage",
                "parent_intent_id",
                "stale_reason",
                "updated_at",
                "oms_status",
                "order_status",
                "desired_state",
                "action_type",
                "reconcile_required",
                "continuation_status",
            ]
        )

    result_rows = []
    if not orders.empty:
        orders["updated_at"] = orders.get("updated_at", "").map(_parse_ts)
        orders["submit_time"] = orders.get("submit_time", "").map(_parse_ts)
    if not fills.empty:
        fills["filled_time"] = fills.get("filled_time", "").map(_parse_ts)
    for _, row in intents.iterrows():
        intent_id = str(row.get("intent_id", "") or "").strip()
        if not intent_id:
            continue
        symbol = str(row.get("symbol", "") or "").strip()
        row_orders = orders.loc[orders.get("intent_id", "").astype(str).eq(intent_id)].copy() if not orders.empty else pd.DataFrame()
        row_fills = fills.loc[fills.get("intent_id", "").astype(str).eq(intent_id)].copy() if not fills.empty else pd.DataFrame()
        latest_order = {}
        if not row_orders.empty:
            row_orders["sort_key"] = row_orders["updated_at"].fillna(row_orders["submit_time"])
            latest_order = dict(row_orders.sort_values("sort_key").iloc[-1].to_dict())
        continuity = dict(continuity_map.get(intent_id, {}) or {})
        oms_status = str(row.get("status", "") or "").strip()
        order_status = str(latest_order.get("status", "") or "").strip()
        reconcile_required = bool(force_reconcile_only or row.get("reconcile_required", False) or continuity.get("reconcile_required", False) or continuity_report.get("global_reconcile_only", False))
        latest_order_ts = latest_order.get("updated_at") or latest_order.get("submit_time")
        stale_reason = ""
        if latest_order_ts and order_status.lower() in {"submitted", "acknowledged", "partial_fill", "cancel_requested"}:
            age = now_value - latest_order_ts
            if age >= timedelta(minutes=max(int(stale_minutes or 20), 1)):
                stale_reason = f"order_age_minutes>={max(int(stale_minutes or 20), 1)}"
        if not stale_reason and str(continuity.get("continuity_status", "") or "") in {"stale_open_order_reconcile"}:
            stale_reason = str(continuity.get("continuity_note", "") or "continuity_stale_open_order")
        is_replace_lineage = bool(str(row.get("supersedes_intent_id", "") or "").strip() or str(row.get("replaced_by_intent_id", "") or "").strip() or intent_id in replace_old_ids)
        fill_qty = float(pd.to_numeric(row_fills.get("filled_qty", 0.0), errors="coerce").fillna(0.0).sum()) if not row_fills.empty else float(pd.to_numeric(latest_order.get("filled_qty", 0.0), errors="coerce"))
        order_qty = float(pd.to_numeric(latest_order.get("qty", 0.0), errors="coerce"))
        fill_ratio = max(min(fill_qty / order_qty, 1.0), 0.0) if order_qty > 0 else (1.0 if oms_status == "filled" else 0.0)

        if reconcile_required:
            intent_state = "reconcile_only"
        elif oms_status == "filled":
            intent_state = "filled"
        elif oms_status == "cancelled":
            intent_state = "cancelled"
        elif oms_status == "cancel_requested" or intent_id in cancel_intent_ids or bool(latest_order.get("cancel_requested", False)):
            intent_state = "cancel_requested"
        elif intent_id in replace_old_ids:
            intent_state = "replace_required"
        elif stale_reason:
            intent_state = "stale_pending"
        elif oms_status == "partial_fill":
            intent_state = "partial_fill"
        elif oms_status == "acknowledged":
            intent_state = "acknowledged"
        elif oms_status == "submitted":
            intent_state = "submitted"
        elif oms_status == "planned":
            if str(row.get("dispatch_block_reason", "") or "").strip():
                intent_state = "aborted"
            elif is_replace_lineage or bool(continuity):
                intent_state = "admitted"
            else:
                intent_state = "planned"
        else:
            intent_state = "aborted"

        result_rows.append(
            {
                "trade_date": str(trade_date or ""),
                "release_id": str(row.get("release_id", "") or release_id or ""),
                "namespace": str(namespace or "main"),
                "intent_id": intent_id,
                "stock_code": symbol,
                "intent_state": intent_state,
                "order_id": str(latest_order.get("order_id", "") or row.get("latest_order_id", "") or ""),
                "fill_ratio": round(float(fill_ratio), 6),
                "is_replace_lineage": bool(is_replace_lineage),
                "parent_intent_id": str(row.get("supersedes_intent_id", "") or ""),
                "stale_reason": stale_reason,
                "updated_at": str(row.get("updated_at", "") or ""),
                "oms_status": oms_status,
                "order_status": order_status,
                "desired_state": str(row.get("desired_state", "") or ""),
                "action_type": str(row.get("action_type", "") or ""),
                "reconcile_required": bool(reconcile_required),
                "continuation_status": str(continuity.get("continuity_status", "") or row.get("continuation_status", "") or ""),
            }
        )
    frame = pd.DataFrame(result_rows)
    if not frame.empty:
        frame = frame.sort_values(["stock_code", "updated_at", "intent_id"]).reset_index(drop=True)
    return frame
