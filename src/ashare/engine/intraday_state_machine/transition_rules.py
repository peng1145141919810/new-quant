from __future__ import annotations

from typing import Dict, List


SYMBOL_TRANSITIONS: Dict[str, List[str]] = {
    "watch": ["pilot_entry", "reconcile_only", "freeze"],
    "pilot_entry": ["build_entry", "hold_manage", "freeze", "reconcile_only"],
    "build_entry": ["hold_manage", "trim_watch", "freeze", "reconcile_only"],
    "hold_manage": ["trim_watch", "exit_execute", "reconcile_only", "freeze"],
    "trim_watch": ["exit_execute", "hold_manage", "reconcile_only", "freeze"],
    "exit_execute": ["reconcile_only", "freeze"],
    "reconcile_only": ["freeze"],
    "freeze": [],
}


INTENT_TRANSITIONS: Dict[str, List[str]] = {
    "planned": ["admitted", "reconcile_only", "aborted"],
    "admitted": ["submitted", "reconcile_only", "aborted"],
    "submitted": ["acknowledged", "stale_pending", "reconcile_only", "aborted"],
    "acknowledged": ["partial_fill", "filled", "stale_pending", "cancel_requested", "reconcile_only", "aborted"],
    "partial_fill": ["filled", "stale_pending", "cancel_requested", "reconcile_only", "aborted"],
    "filled": [],
    "stale_pending": ["replace_required", "cancel_requested", "reconcile_only", "aborted"],
    "replace_required": ["cancel_requested", "reconcile_only", "aborted"],
    "cancel_requested": ["cancelled", "reconcile_only", "aborted"],
    "cancelled": ["admitted"],
    "reconcile_only": ["aborted"],
    "aborted": [],
}


SYMBOL_EVENT_ACTION_MATRIX = [
    {"from_state": "watch", "event": "phase_entered:morning_probe", "action": "allow_pilot_probe", "to_state": "pilot_entry"},
    {"from_state": "pilot_entry", "event": "partial_fill_detected", "action": "hold_or_continue", "to_state": "hold_manage"},
    {"from_state": "build_entry", "event": "safety_changed:PANIC", "action": "block_build_keep_reduce", "to_state": "trim_watch"},
    {"from_state": "hold_manage", "event": "midday_plan_published:risk_reduce", "action": "trim_followup", "to_state": "trim_watch"},
    {"from_state": "trim_watch", "event": "fill_completed", "action": "exit_or_stabilize", "to_state": "exit_execute"},
    {"from_state": "any", "event": "manual_override_applied", "action": "freeze_symbol", "to_state": "freeze"},
    {"from_state": "any", "event": "close_reconcile_started", "action": "reconcile_only", "to_state": "reconcile_only"},
]


INTENT_EVENT_ACTION_MATRIX = [
    {"from_state": "planned", "event": "intent_admitted", "action": "allow_submit", "to_state": "admitted"},
    {"from_state": "admitted", "event": "order_submitted", "action": "track_broker_submit", "to_state": "submitted"},
    {"from_state": "submitted", "event": "order_acknowledged", "action": "track_ack", "to_state": "acknowledged"},
    {"from_state": "acknowledged", "event": "partial_fill_detected", "action": "track_partial", "to_state": "partial_fill"},
    {"from_state": "acknowledged", "event": "stale_pending_detected", "action": "mark_stale", "to_state": "stale_pending"},
    {"from_state": "stale_pending", "event": "replace_required_detected", "action": "queue_replace", "to_state": "replace_required"},
    {"from_state": "replace_required", "event": "cancel_requested", "action": "request_cancel", "to_state": "cancel_requested"},
    {"from_state": "cancel_requested", "event": "cancel_confirmed", "action": "close_old_intent", "to_state": "cancelled"},
    {"from_state": "any", "event": "close_reconcile_started", "action": "force_reconcile_only", "to_state": "reconcile_only"},
]


def transition_allowed(mapping: Dict[str, List[str]], from_state: str, to_state: str) -> bool:
    source = str(from_state or "").strip()
    target = str(to_state or "").strip()
    if not source or not target:
        return False
    return target in list(mapping.get(source, []) or [])
