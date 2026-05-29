from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from ..contracts.intent_schema import INTENT_LEDGER_FIELDS, OPEN_INTENT_STATUSES


BUY_ACTIONS = {"new", "add"}
SELL_ACTIONS = {"trim", "exit"}
NOOP_ACTIONS = {"hold", "watch"}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_datetime(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.Timestamp("1970-01-01")
    return parsed


def _order_key(row: Dict[str, Any]) -> str:
    return str(row.get("order_id", "") or row.get("cl_ord_id", "") or "").strip()


def _same_direction(old_action: str, new_action: str) -> bool:
    if old_action in BUY_ACTIONS and new_action in BUY_ACTIONS:
        return True
    if old_action in SELL_ACTIONS and new_action in SELL_ACTIONS:
        return True
    return False


def _normalize_override_controls(overrides: Dict[str, Any]) -> Dict[str, Any]:
    intent_controls = dict(overrides.get("intent_controls", {}) or {})
    symbol_controls = dict(overrides.get("symbol_controls", {}) or {})
    session_controls = dict(overrides.get("session_controls", {}) or {})
    return {
        "force_close_intents": {str(item).strip() for item in list(intent_controls.get("force_close_intents", []) or []) if str(item).strip()},
        "force_expire_intents": {str(item).strip() for item in list(intent_controls.get("force_expire_intents", []) or []) if str(item).strip()},
        "operator_cancel_intents": {str(item).strip() for item in list(intent_controls.get("operator_cancel_intents", []) or []) if str(item).strip()},
        "freeze_intent_continuation": {str(item).strip().upper() for item in list(intent_controls.get("freeze_intent_continuation", []) or []) if str(item).strip()},
        "reconcile_required_symbols": {str(item).strip().upper() for item in list(symbol_controls.get("reconcile_required_symbols", []) or []) if str(item).strip()},
        "force_reconcile_only": bool(session_controls.get("force_reconcile_only", False)),
    }


def build_intent_continuity(
    actual_state_frame: pd.DataFrame,
    previous_intent_frame: pd.DataFrame,
    previous_order_frame: pd.DataFrame,
    unfinished_orders: List[Dict[str, Any]],
    release_id: str,
    overrides: Dict[str, Any],
    oms_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    frame = actual_state_frame.copy() if actual_state_frame is not None else pd.DataFrame()
    previous = previous_intent_frame.copy() if previous_intent_frame is not None else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    now_text = _now_text()
    controls = _normalize_override_controls(overrides=overrides)

    current_by_symbol = {
        str(row.get("symbol", "") or "").strip().upper(): row.to_dict()
        for _, row in frame.iterrows()
        if str(row.get("symbol", "") or "").strip()
    }
    unfinished_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    unfinished_by_key: Dict[str, Dict[str, Any]] = {}
    for item in list(unfinished_orders or []):
        row = dict(item)
        symbol = str(row.get("symbol", "") or "").strip().upper()
        if symbol:
            unfinished_by_symbol.setdefault(symbol, []).append(row)
        key = _order_key(row)
        if key:
            unfinished_by_key[key] = row

    if not previous.empty:
        for col in INTENT_LEDGER_FIELDS:
            if col not in previous.columns:
                previous[col] = pd.NA
        previous = previous[INTENT_LEDGER_FIELDS].copy()
        previous["updated_sort"] = previous["updated_at"].map(_to_datetime)
        previous = previous.sort_values(["symbol", "updated_sort", "created_at"])
    latest_open_by_symbol: Dict[str, Dict[str, Any]] = {}
    if not previous.empty:
        open_rows = previous.loc[previous["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy()
        if not open_rows.empty:
            latest_open_by_symbol = {}
            for _, row in open_rows.iterrows():
                payload = row.to_dict()
                payload.pop("updated_sort", None)
                latest_open_by_symbol[str(row["symbol"]).strip().upper()] = payload

    updated_previous = previous.drop(columns=["updated_sort"]) if "updated_sort" in previous.columns else previous.copy()
    carried_intent_by_symbol: Dict[str, Dict[str, Any]] = {}
    replacement_required_by_symbol: Dict[str, str] = {}
    cancel_requests: List[Dict[str, Any]] = []
    continuity_rows: List[Dict[str, Any]] = []

    for symbol, payload in latest_open_by_symbol.items():
        current = dict(current_by_symbol.get(symbol, {}) or {})
        updated = dict(payload)
        previous_action = str(payload.get("action_type", "") or "")
        current_action = str(current.get("action_type", "") or "")
        latest_order_id = str(payload.get("latest_order_id", "") or "")
        latest_order_row = dict(unfinished_by_key.get(latest_order_id, {}) or {})
        has_open_order = bool(latest_order_row) or bool(unfinished_by_symbol.get(symbol))
        reconcile_required = bool(current.get("reconcile_required", False)) or symbol in controls["reconcile_required_symbols"]
        continuity_status = "resume_unknown"
        continuity_note = ""
        block_dispatch = False
        dispatch_block_reason = ""
        cancel_reason = ""

        if str(updated.get("intent_id", "") or "") in controls["force_close_intents"]:
            updated["status"] = "operator_closed"
            updated["manual_override_flag"] = True
            continuity_status = "operator_closed"
            continuity_note = "manual_force_close_intent"
            cancel_reason = continuity_note
        elif str(updated.get("intent_id", "") or "") in controls["force_expire_intents"]:
            updated["status"] = "expired"
            updated["manual_override_flag"] = True
            continuity_status = "operator_expired"
            continuity_note = "manual_force_expire_intent"
            cancel_reason = continuity_note
        elif str(updated.get("intent_id", "") or "") in controls["operator_cancel_intents"]:
            updated["status"] = "operator_cancelled"
            updated["manual_override_flag"] = True
            continuity_status = "operator_cancelled"
            continuity_note = "manual_operator_cancel_intent"
            cancel_reason = continuity_note
        elif _to_datetime(updated.get("expires_at", "")) < _to_datetime(now_text):
            updated["status"] = "expired"
            continuity_status = "expired"
            continuity_note = "intent_expired_by_age"
            cancel_reason = continuity_note
        elif symbol in controls["freeze_intent_continuation"]:
            updated["status"] = "operator_closed"
            updated["manual_override_flag"] = True
            continuity_status = "operator_freeze_continuation"
            continuity_note = "manual_freeze_intent_continuation"
            cancel_reason = continuity_note
        elif not current:
            if has_open_order:
                continuity_status = "stale_open_order_reconcile"
                continuity_note = "open_order_without_current_gap"
                block_dispatch = True
                dispatch_block_reason = "continuity_open_order_without_gap"
                reconcile_required = True
            else:
                updated["status"] = "filled"
                continuity_status = "resolved_without_gap"
                continuity_note = "current_gap_resolved"
        elif current_action in NOOP_ACTIONS and abs(float(current.get("gap_weight", 0.0) or 0.0)) <= 1e-6:
            if has_open_order:
                continuity_status = "cancel_on_converged_gap"
                continuity_note = "desired_gap_converged_but_order_open"
                cancel_reason = continuity_note
                block_dispatch = True
                dispatch_block_reason = "continuity_converged_gap_open_order"
            else:
                updated["status"] = "filled"
                continuity_status = "converged"
                continuity_note = "actual_converged_to_desired"
        elif _same_direction(previous_action, current_action):
            updated["release_id"] = str(release_id or updated.get("release_id", "") or "")
            updated["origin_release_id"] = str(updated.get("origin_release_id", "") or payload.get("release_id", "") or release_id)
            updated["continuation_count"] = int(updated.get("continuation_count", 0) or 0) + 1
            if has_open_order:
                continuity_status = "continue_with_open_order"
                continuity_note = "existing_unfinished_order_carries_forward"
                block_dispatch = True
                dispatch_block_reason = "continuity_existing_unfinished_order"
            elif controls["force_reconcile_only"] or reconcile_required:
                continuity_status = "continue_reconcile_only"
                continuity_note = "continuation_blocked_by_reconcile_only"
                block_dispatch = True
                dispatch_block_reason = "continuity_reconcile_only"
            else:
                continuity_status = "continue_residual"
                continuity_note = "residual_gap_continues_same_intent"
            carried_intent_by_symbol[symbol] = {
                "payload": updated,
                "block_dispatch": block_dispatch,
                "dispatch_block_reason": dispatch_block_reason,
                "continuity_status": continuity_status,
                "continuity_note": continuity_note,
                "reconcile_required": reconcile_required,
            }
        else:
            updated["status"] = "superseded"
            continuity_status = "superseded_by_new_release"
            continuity_note = f"{previous_action}->{current_action}"
            replacement_required_by_symbol[symbol] = str(updated.get("intent_id", "") or "")
            if has_open_order:
                cancel_reason = "superseded_contradictory_release"

        updated["continuation_status"] = continuity_status
        updated["continuation_note"] = continuity_note
        updated["reconcile_required"] = bool(reconcile_required)
        updated["updated_at"] = now_text
        if continuity_status.startswith("continue_") and symbol in carried_intent_by_symbol:
            carried_intent_by_symbol[symbol]["payload"] = updated

        if cancel_reason:
            request = {
                "symbol": symbol,
                "intent_id": str(updated.get("intent_id", "") or ""),
                "order_id": latest_order_id,
                "cl_ord_id": str(latest_order_row.get("cl_ord_id", "") or ""),
                "reason": cancel_reason,
            }
            if request["order_id"] or request["cl_ord_id"]:
                cancel_requests.append(request)

        continuity_rows.append(
            {
                "symbol": symbol,
                "intent_id": str(updated.get("intent_id", "") or ""),
                "previous_release_id": str(payload.get("release_id", "") or ""),
                "current_release_id": str(release_id or ""),
                "previous_action_type": previous_action,
                "current_action_type": current_action,
                "has_open_order": has_open_order,
                "reconcile_required": bool(reconcile_required),
                "continuity_status": continuity_status,
                "continuity_note": continuity_note,
                "cancel_requested": bool(cancel_reason),
            }
        )

        if not updated_previous.empty:
            mask = updated_previous["intent_id"].astype(str) == str(updated.get("intent_id", "") or "")
            if mask.any():
                for key, value in updated.items():
                    if key in updated_previous.columns:
                        updated_previous.loc[mask, key] = value

    open_intents_frame = updated_previous.loc[updated_previous["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy() if not updated_previous.empty else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    continuity_report = {
        "generated_at": now_text,
        "release_id": str(release_id or ""),
        "global_reconcile_only": bool(controls["force_reconcile_only"]),
        "rows": continuity_rows,
        "summary": {
            "n_open_intents_before": int(len(latest_open_by_symbol)),
            "n_open_intents_after": int(len(open_intents_frame.index)),
            "n_carried_symbols": int(len(carried_intent_by_symbol)),
            "n_replacements": int(len(replacement_required_by_symbol)),
            "n_cancel_requests": int(len(cancel_requests)),
            "n_reconcile_required": int(sum(bool(row.get("reconcile_required", False)) for row in continuity_rows)),
        },
    }
    session_resume_audit = {
        "generated_at": now_text,
        "release_id": str(release_id or ""),
        "global_reconcile_only": bool(controls["force_reconcile_only"]),
        "carried_symbols": sorted(list(carried_intent_by_symbol.keys())),
        "replacement_required_symbols": sorted(list(replacement_required_by_symbol.keys())),
        "cancel_requests": cancel_requests,
        "summary": dict(continuity_report["summary"]),
    }
    return {
        "updated_previous_intents": updated_previous[INTENT_LEDGER_FIELDS].copy() if not updated_previous.empty else pd.DataFrame(columns=INTENT_LEDGER_FIELDS),
        "carried_intent_by_symbol": carried_intent_by_symbol,
        "replacement_required_by_symbol": replacement_required_by_symbol,
        "cancel_requests": cancel_requests,
        "open_intents_frame": open_intents_frame[INTENT_LEDGER_FIELDS].copy() if not open_intents_frame.empty else pd.DataFrame(columns=INTENT_LEDGER_FIELDS),
        "continuity_report": continuity_report,
        "session_resume_audit": session_resume_audit,
        "global_reconcile_only": bool(controls["force_reconcile_only"]),
    }
