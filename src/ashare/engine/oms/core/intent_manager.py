from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from uuid import uuid4

import pandas as pd

from live_execution_bridge.models import OrderIntent
from live_execution_bridge.utils import safe_float, safe_int
from ..contracts.fill_schema import FILL_LEDGER_FIELDS
from ..contracts.intent_schema import INTENT_LEDGER_FIELDS, OPEN_INTENT_STATUSES
from ..contracts.order_schema import ORDER_LEDGER_FIELDS


TERMINAL_INTENT_STATUSES = {
    "filled",
    "cancelled",
    "operator_cancelled",
    "operator_closed",
    "rejected",
    "expired",
    "superseded",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _intent_priority(action_type: str, gap_weight_abs: float) -> float:
    base = {
        "exit": 400.0,
        "trim": 320.0,
        "new": 260.0,
        "add": 220.0,
        "hold": 120.0,
        "watch": 80.0,
    }.get(str(action_type or ""), 100.0)
    return round(base + float(gap_weight_abs or 0.0) * 100.0, 6)


def _intent_urgency(action_type: str, gap_weight_abs: float) -> float:
    base = {
        "exit": 0.95,
        "trim": 0.78,
        "new": 0.64,
        "add": 0.58,
        "hold": 0.20,
        "watch": 0.10,
    }.get(str(action_type or ""), 0.15)
    return round(min(max(base + float(gap_weight_abs or 0.0), 0.0), 1.0), 6)


def _symbol_controls(overrides: Dict[str, Any]) -> Dict[str, set[str]]:
    symbol_controls = dict(overrides.get("symbol_controls", {}) or {})
    return {
        "freeze_new": {str(item).strip().upper() for item in list(symbol_controls.get("freeze_new_entry_symbols", []) or []) if str(item).strip()},
        "freeze_build": {str(item).strip().upper() for item in list(symbol_controls.get("freeze_build_symbols", []) or []) if str(item).strip()},
        "reconcile_required": {str(item).strip().upper() for item in list(symbol_controls.get("reconcile_required_symbols", []) or []) if str(item).strip()},
    }


def _previous_latest_by_symbol(previous: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    if previous is None or previous.empty or "symbol" not in previous.columns:
        return {}
    bucket = previous.copy()
    bucket["updated_sort"] = pd.to_datetime(bucket.get("updated_at", ""), errors="coerce")
    bucket = bucket.sort_values(["symbol", "updated_sort", "created_at"])
    return {
        str(row["symbol"]).strip().upper(): row.to_dict()
        for _, row in bucket.iterrows()
        if str(row.get("symbol", "") or "").strip()
    }


def build_intent_plan(
    actual_state_frame: pd.DataFrame,
    control_result: Dict[str, Any],
    previous_intent_frame: pd.DataFrame,
    release_id: str,
    overrides: Dict[str, Any],
    oms_cfg: Dict[str, Any],
    continuity: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    frame = actual_state_frame.copy() if actual_state_frame is not None else pd.DataFrame()
    previous = previous_intent_frame.copy() if previous_intent_frame is not None else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    continuity = dict(continuity or {})
    now_text = _now_text()
    expiry_text = (datetime.now() + timedelta(days=int(oms_cfg.get("intent_expiry_days", 3) or 3))).strftime("%Y-%m-%d %H:%M:%S")
    final_orders = list(control_result.get("final_orders", []) or [])
    symbol_to_order = {str(order.symbol).strip().upper(): order for order in final_orders}
    symbol_controls = _symbol_controls(overrides=overrides)
    force_reconcile_only = bool(continuity.get("global_reconcile_only", False))
    carried_by_symbol = dict(continuity.get("carried_intent_by_symbol", {}) or {})
    replacement_required_by_symbol = dict(continuity.get("replacement_required_by_symbol", {}) or {})
    latest_previous_by_symbol = _previous_latest_by_symbol(previous)

    rows: List[Dict[str, Any]] = []
    order_to_intent: Dict[Tuple[str, str], str] = {}
    dispatch_orders: List[OrderIntent] = []
    dispatch_blocked: List[Dict[str, Any]] = []
    replacement_links: List[Dict[str, Any]] = []

    for _, row in frame.iterrows():
        symbol = str(row.get("symbol", "") or "").strip().upper()
        if not symbol:
            continue
        action_type = str(row.get("action_type", "") or "")
        target_weight = safe_float(row.get("target_weight", 0.0), 0.0)
        actual_weight = safe_float(row.get("actual_weight", 0.0), 0.0)
        delta_weight = round(target_weight - actual_weight, 6)
        delta_shares = safe_int(row.get("gap_shares", 0), 0)
        planned_order = symbol_to_order.get(symbol)
        carried = dict(carried_by_symbol.get(symbol, {}) or {})
        existing = dict(carried.get("payload", {}) or latest_previous_by_symbol.get(symbol, {}) or {})
        if supersedes_intent_id := str(replacement_required_by_symbol.get(symbol, "") or ""):
            existing = {}
        elif not carried and str(existing.get("status", "") or "") in TERMINAL_INTENT_STATUSES:
            existing = {}
        intent_id = str(existing.get("intent_id", "") or "") or f"intent_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        origin_release_id = str(existing.get("origin_release_id", "") or existing.get("release_id", "") or release_id)
        continuation_status = str(carried.get("continuity_status", existing.get("continuation_status", "fresh")) or "fresh")
        continuation_note = str(carried.get("continuity_note", existing.get("continuation_note", "")) or "")
        continuation_count = int(existing.get("continuation_count", 0) or 0)
        dispatch_block_reason = ""
        status = str(existing.get("status", "") or "")
        manual_override_flag = bool(existing.get("manual_override_flag", False))
        reconcile_required = bool(row.get("reconcile_required", False)) or symbol in symbol_controls["reconcile_required"] or bool(carried.get("reconcile_required", False))

        if force_reconcile_only:
            dispatch_block_reason = "manual_force_reconcile_only"
        elif reconcile_required:
            dispatch_block_reason = "manual_reconcile_required"
        elif carried:
            dispatch_block_reason = str(carried.get("dispatch_block_reason", "") or "")
        elif symbol in symbol_controls["freeze_new"] and action_type == "new":
            dispatch_block_reason = "manual_symbol_freeze_new_entry"
        elif symbol in symbol_controls["freeze_build"] and action_type in {"new", "add"}:
            dispatch_block_reason = "manual_symbol_freeze_build"

        if dispatch_block_reason:
            manual_override_flag = manual_override_flag or dispatch_block_reason.startswith("manual_")

        if supersedes_intent_id and not str(existing.get("intent_id", "") or ""):
            existing = {}

        if planned_order is not None and not dispatch_block_reason:
            status = "planned"
            dispatch_orders.append(planned_order)
            order_to_intent[(symbol, str(planned_order.side).upper())] = intent_id
        elif carried:
            status = str(existing.get("status", "") or "planned")
        elif str(existing.get("status", "") or "") in OPEN_INTENT_STATUSES:
            status = str(existing.get("status", "") or "planned")
        elif action_type in {"hold", "watch"} or abs(delta_weight) <= 1e-6:
            status = "filled"
        elif dispatch_block_reason:
            status = "planned"
        else:
            status = "planned"
            dispatch_block_reason = "not_selected_for_dispatch"

        if dispatch_block_reason:
            dispatch_blocked.append(
                {
                    "symbol": symbol,
                    "reason": dispatch_block_reason,
                    "planned_shares": delta_shares,
                    "final_shares": 0,
                }
            )

        if supersedes_intent_id and supersedes_intent_id != intent_id:
            replacement_links.append({"old_intent_id": supersedes_intent_id, "new_intent_id": intent_id, "symbol": symbol})

        rows.append(
            {
                "intent_id": intent_id,
                "release_id": str(release_id or ""),
                "origin_release_id": origin_release_id,
                "symbol": symbol,
                "action_type": action_type,
                "desired_state": str(row.get("desired_state", "") or ""),
                "actual_state": str(row.get("actual_state", "") or ""),
                "target_weight_before": actual_weight,
                "target_weight_after": target_weight,
                "delta_weight": delta_weight,
                "delta_shares": delta_shares,
                "priority": _intent_priority(action_type=action_type, gap_weight_abs=safe_float(row.get("gap_weight_abs", 0.0), 0.0)),
                "urgency": _intent_urgency(action_type=action_type, gap_weight_abs=safe_float(row.get("gap_weight_abs", 0.0), 0.0)),
                "reason": str(row.get("state_gap_reason", "") or ""),
                "status": status,
                "dispatch_block_reason": dispatch_block_reason,
                "continuation_status": continuation_status,
                "continuation_count": continuation_count,
                "continuation_note": continuation_note,
                "supersedes_intent_id": supersedes_intent_id,
                "replaced_by_intent_id": "",
                "manual_override_flag": bool(manual_override_flag),
                "reconcile_required": bool(reconcile_required),
                "created_at": str(existing.get("created_at", "") or now_text),
                "updated_at": now_text,
                "expires_at": str(existing.get("expires_at", "") or expiry_text),
                "latest_order_id": str(existing.get("latest_order_id", "") or ""),
                "latest_fill_id": str(existing.get("latest_fill_id", "") or ""),
            }
        )
    intent_frame = pd.DataFrame(rows)
    for col in INTENT_LEDGER_FIELDS:
        if col not in intent_frame.columns:
            intent_frame[col] = pd.NA
    return {
        "intent_frame": intent_frame[INTENT_LEDGER_FIELDS].copy(),
        "dispatch_orders": dispatch_orders,
        "dispatch_blocked": dispatch_blocked,
        "order_to_intent": order_to_intent,
        "replacement_links": replacement_links,
    }


def merge_order_ledger(
    previous_order_frame: pd.DataFrame,
    submitted_orders: List[Dict[str, Any]],
    day_orders: List[Dict[str, Any]],
    unfinished_orders: List[Dict[str, Any]],
    order_to_intent: Dict[Tuple[str, str], str],
    release_id: str,
    overrides: Dict[str, Any] | None = None,
    cancel_requests: List[Dict[str, Any]] | None = None,
    cancel_results: List[Dict[str, Any]] | None = None,
) -> pd.DataFrame:
    previous = previous_order_frame.copy() if previous_order_frame is not None else pd.DataFrame(columns=ORDER_LEDGER_FIELDS)
    rows: Dict[str, Dict[str, Any]] = {}
    if not previous.empty and "order_id" in previous.columns:
        rows = {str(row["order_id"]): row.to_dict() for _, row in previous.iterrows()}
    now_text = _now_text()
    day_map = {
        str(row.get("order_id", "") or ""): dict(row)
        for row in list(day_orders or [])
        if str(row.get("order_id", "") or "")
    }
    unfinished_ids = {str(row.get("order_id", "") or "") for row in list(unfinished_orders or [])}
    expire_orders = {str(item).strip() for item in list((overrides or {}).get("session_controls", {}).get("expire_orders", []) or []) if str(item).strip()}

    for submitted in list(submitted_orders or []):
        symbol = str(submitted.get("symbol", "") or "").strip().upper()
        side = str(submitted.get("side", "") or "").upper()
        order_id = str(submitted.get("order_id", "") or "") or f"order_{uuid4().hex[:10]}"
        day_row = dict(day_map.get(order_id, {}) or {})
        status_name = str(day_row.get("status_name", "") or "")
        if not status_name:
            status_name = "submitted"
        elif order_id in unfinished_ids and status_name in {"New", "PendingNew"}:
            status_name = "acknowledged"
        elif order_id in unfinished_ids and status_name in {"PartiallyFilled", "PendingCancel"}:
            status_name = "partial_fill" if status_name == "PartiallyFilled" else "cancel_requested"
        elif status_name == "Filled":
            status_name = "filled"
        elif status_name == "Rejected":
            status_name = "rejected"
        elif status_name in {"Canceled", "Expired"}:
            status_name = status_name.lower()
        if order_id in expire_orders:
            status_name = "expired"
        record = {
            "order_id": order_id,
            "intent_id": str(order_to_intent.get((symbol, side), "") or rows.get(order_id, {}).get("intent_id", "") or ""),
            "release_id": str(release_id or rows.get(order_id, {}).get("release_id", "") or ""),
            "symbol": symbol,
            "broker_order_id": order_id,
            "cl_ord_id": str(submitted.get("cl_ord_id", "") or rows.get(order_id, {}).get("cl_ord_id", "") or ""),
            "submit_time": str(rows.get(order_id, {}).get("submit_time", "") or now_text),
            "side": side,
            "price_type": "limit",
            "qty": int(submitted.get("delta_shares", 0) or rows.get(order_id, {}).get("qty", 0) or 0),
            "filled_qty": int(day_row.get("filled_volume", rows.get(order_id, {}).get("filled_qty", 0)) or 0),
            "remaining_qty": int(day_row.get("remaining_shares", rows.get(order_id, {}).get("remaining_qty", 0)) or max(int(submitted.get("delta_shares", 0) or 0) - int(day_row.get("filled_volume", 0) or 0), 0)),
            "submit_price": safe_float(submitted.get("submit_price", rows.get(order_id, {}).get("submit_price", 0.0)), 0.0),
            "status": status_name,
            "status_reason": str(day_row.get("status_detail", rows.get(order_id, {}).get("status_reason", "")) or ""),
            "cancel_requested": bool(rows.get(order_id, {}).get("cancel_requested", False)),
            "cancel_requested_at": str(rows.get(order_id, {}).get("cancel_requested_at", "") or ""),
            "cancel_reason": str(rows.get(order_id, {}).get("cancel_reason", "") or ""),
            "cancel_result": str(rows.get(order_id, {}).get("cancel_result", "") or ""),
            "updated_at": now_text,
        }
        rows[order_id] = {**rows.get(order_id, {}), **record}

    cancel_result_map = {
        str(item.get("order_id", "") or ""): dict(item)
        for item in list(cancel_results or [])
        if str(item.get("order_id", "") or "")
    }
    for request in list(cancel_requests or []):
        order_id = str(request.get("order_id", "") or "").strip()
        if not order_id:
            continue
        existing = dict(rows.get(order_id, day_map.get(order_id, {})) or {})
        result = dict(cancel_result_map.get(order_id, {}) or {})
        status_name = str(existing.get("status", "") or "cancel_requested")
        if result:
            result_status = str(result.get("status", "") or "").strip().lower()
            if result_status == "cancelled":
                status_name = "cancelled"
            elif result_status == "rejected":
                status_name = "rejected"
            elif result_status == "accepted":
                status_name = "cancel_requested"
        if str(existing.get("status", "") or "") in {"cancelled", "filled", "expired"}:
            status_name = str(existing.get("status", "") or status_name)
        rows[order_id] = {
            **existing,
            "order_id": order_id,
            "intent_id": str(existing.get("intent_id", request.get("intent_id", "")) or ""),
            "release_id": str(existing.get("release_id", release_id) or release_id),
            "symbol": str(existing.get("symbol", request.get("symbol", "")) or "").strip().upper(),
            "broker_order_id": str(existing.get("broker_order_id", order_id) or order_id),
            "cl_ord_id": str(existing.get("cl_ord_id", request.get("cl_ord_id", "")) or ""),
            "submit_time": str(existing.get("submit_time", now_text) or now_text),
            "side": str(existing.get("side", "") or ""),
            "price_type": str(existing.get("price_type", "limit") or "limit"),
            "qty": int(existing.get("qty", 0) or 0),
            "filled_qty": int(existing.get("filled_qty", 0) or 0),
            "remaining_qty": int(existing.get("remaining_qty", 0) or 0),
            "submit_price": safe_float(existing.get("submit_price", 0.0), 0.0),
            "status": status_name,
            "status_reason": str(existing.get("status_reason", "") or request.get("reason", "") or ""),
            "cancel_requested": True,
            "cancel_requested_at": now_text,
            "cancel_reason": str(request.get("reason", "") or ""),
            "cancel_result": str(result.get("result_text", result.get("status", "")) or ""),
            "updated_at": now_text,
        }

    out = pd.DataFrame(list(rows.values()))
    for col in ORDER_LEDGER_FIELDS:
        if col not in out.columns:
            out[col] = pd.NA
    return out[ORDER_LEDGER_FIELDS].copy()


def merge_fill_ledger(
    previous_fill_frame: pd.DataFrame,
    fill_rows: List[Dict[str, Any]],
    order_frame: pd.DataFrame,
) -> pd.DataFrame:
    previous = previous_fill_frame.copy() if previous_fill_frame is not None else pd.DataFrame(columns=FILL_LEDGER_FIELDS)
    rows: Dict[str, Dict[str, Any]] = {}
    if not previous.empty and "fill_id" in previous.columns:
        rows = {
            str(row["fill_id"]): row.to_dict()
            for _, row in previous.iterrows()
        }
    order_map = {}
    if order_frame is not None and not order_frame.empty and "order_id" in order_frame.columns:
        order_map = {
            str(row["order_id"]): row.to_dict()
            for _, row in order_frame.iterrows()
        }
    for row in list(fill_rows or []):
        fill_id = str(row.get("fill_id", "") or row.get("exec_id", "") or "") or f"fill_{uuid4().hex[:10]}"
        order_id = str(row.get("order_id", "") or "")
        order_ref = dict(order_map.get(order_id, {}) or {})
        rows[fill_id] = {
            "fill_id": fill_id,
            "order_id": order_id,
            "intent_id": str(order_ref.get("intent_id", "") or ""),
            "release_id": str(order_ref.get("release_id", "") or ""),
            "symbol": str(row.get("symbol", "") or "").strip().upper(),
            "side": str(row.get("side", "") or ""),
            "filled_qty": int(row.get("filled_qty", row.get("shares", 0)) or 0),
            "filled_price": safe_float(row.get("filled_price", row.get("price", 0.0)), 0.0),
            "filled_amount": safe_float(row.get("filled_amount", row.get("gross_amount", 0.0)), 0.0),
            "fee": safe_float(row.get("fee", 0.0), 0.0),
            "filled_time": str(row.get("filled_time", "") or row.get("exec_time", "") or ""),
        }
    out = pd.DataFrame(list(rows.values()))
    for col in FILL_LEDGER_FIELDS:
        if col not in out.columns:
            out[col] = pd.NA
    return out[FILL_LEDGER_FIELDS].copy()


def finalize_intent_ledger(
    intent_frame: pd.DataFrame,
    order_frame: pd.DataFrame,
    fill_frame: pd.DataFrame,
) -> pd.DataFrame:
    intents = intent_frame.copy() if intent_frame is not None else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    if intents.empty:
        return pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    order_groups = {}
    if order_frame is not None and not order_frame.empty and "intent_id" in order_frame.columns:
        order_groups = {intent_id: bucket.copy() for intent_id, bucket in order_frame.groupby(order_frame["intent_id"].astype(str))}
    fill_groups = {}
    if fill_frame is not None and not fill_frame.empty and "intent_id" in fill_frame.columns:
        fill_groups = {intent_id: bucket.copy() for intent_id, bucket in fill_frame.groupby(fill_frame["intent_id"].astype(str))}
    now_text = _now_text()
    finalized_rows: List[Dict[str, Any]] = []
    for _, row in intents.iterrows():
        payload = row.to_dict()
        intent_id = str(payload.get("intent_id", "") or "")
        orders = order_groups.get(intent_id, pd.DataFrame())
        fills = fill_groups.get(intent_id, pd.DataFrame())
        status = str(payload.get("status", "") or "planned")
        latest_order_id = str(payload.get("latest_order_id", "") or "")
        latest_fill_id = str(payload.get("latest_fill_id", "") or "")
        if not orders.empty:
            latest_order_id = str(orders.iloc[-1].get("order_id", "") or "")
            statuses = set(orders["status"].astype(str).tolist())
            if "rejected" in statuses:
                status = "rejected"
            elif "expired" in statuses:
                status = "expired"
            elif "cancelled" in statuses or "Canceled" in statuses:
                status = "cancelled"
            elif "cancel_requested" in statuses or "PendingCancel" in statuses:
                status = "cancel_requested"
            elif "filled" in statuses:
                total_qty = int(pd.to_numeric(orders.get("qty", 0), errors="coerce").fillna(0).sum())
                total_filled = int(pd.to_numeric(orders.get("filled_qty", 0), errors="coerce").fillna(0).sum())
                status = "filled" if total_filled >= total_qty > 0 else "partial_fill"
            elif "partial_fill" in statuses or "PartiallyFilled" in statuses:
                status = "partial_fill"
            elif "acknowledged" in statuses or "New" in statuses or "PendingNew" in statuses:
                status = "acknowledged"
            elif str(status) not in TERMINAL_INTENT_STATUSES:
                status = "submitted"
        if not fills.empty:
            latest_fill_id = str(fills.iloc[-1].get("fill_id", "") or "")
            if str(status) in {"planned", "submitted", "acknowledged", "cancel_requested"}:
                status = "partial_fill"
        payload["status"] = status
        payload["updated_at"] = now_text
        payload["latest_order_id"] = latest_order_id
        payload["latest_fill_id"] = latest_fill_id
        finalized_rows.append(payload)
    out = pd.DataFrame(finalized_rows)
    for col in INTENT_LEDGER_FIELDS:
        if col not in out.columns:
            out[col] = pd.NA
    return out[INTENT_LEDGER_FIELDS].copy()
