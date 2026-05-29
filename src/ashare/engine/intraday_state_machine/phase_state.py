from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .safety_mapping import allowed_action_bands_for_safety


FORMAL_PHASES = [
    "preopen_prepare",
    "morning_probe",
    "morning_observe",
    "midday_review",
    "afternoon_adjust",
    "close_reconcile",
    "postclose_archive",
]
FORMAL_PHASE_INDEX = {name: idx for idx, name in enumerate(FORMAL_PHASES)}
MIDDAY_DECISIONS = [
    "carry_and_reconcile",
    "continue_build",
    "risk_reduce",
    "abort_new_entries",
]
FINAL_PHASE_STATUSES = {"success", "failed", "skipped", "timeout"}

PHASE_ACTION_MATRIX: Dict[str, Dict[str, List[str]]] = {
    "preopen_prepare": {
        "allowed": ["plan_only", "release_loaded", "safety_review"],
        "prohibited": ["pilot_entry", "build_entry", "trim_watch", "exit_execute"],
    },
    "morning_probe": {
        "allowed": ["pilot_entry", "trim_watch", "exit_execute", "reconcile_only"],
        "prohibited": ["build_entry"],
    },
    "morning_observe": {
        "allowed": ["hold_manage", "reconcile_only", "freeze"],
        "prohibited": ["build_entry", "aggressive_replace_loop"],
    },
    "midday_review": {
        "allowed": MIDDAY_DECISIONS,
        "prohibited": ["free_text_unbounded_decision"],
    },
    "afternoon_adjust": {
        "allowed": ["build_entry", "trim_watch", "exit_execute", "reconcile_only", "freeze"],
        "prohibited": ["new_unbounded_thesis_research"],
    },
    "close_reconcile": {
        "allowed": ["reconcile_only", "cancel_replace_cleanup", "exit_execute", "freeze"],
        "prohibited": ["new_build_entry", "new_pilot_entry"],
    },
    "postclose_archive": {
        "allowed": ["archive_only"],
        "prohibited": ["execution_dispatch"],
    },
}


def derive_midday_decision(midday_plan: Dict[str, Any], safety_mode: str) -> str:
    real_plan = dict(midday_plan.get("real_execution", {}) or {})
    action = str(real_plan.get("action", "") or "").strip()
    if str(safety_mode or "").upper() == "HALT":
        return "abort_new_entries"
    if str(safety_mode or "").upper() == "PANIC":
        return "risk_reduce"
    if action == "carry_and_reconcile":
        return "carry_and_reconcile"
    if action == "followup_execute":
        return "continue_build"
    if bool(real_plan.get("should_run", False)):
        return "continue_build"
    return "abort_new_entries"


def phase_allowed_action_bands(phase_name: str, safety_mode: str, midday_decision: str = "") -> List[str]:
    phase = str(phase_name or "").strip() or "preopen_prepare"
    allowed = list(dict(PHASE_ACTION_MATRIX.get(phase, {}) or {}).get("allowed", []) or [])
    safety_allowed = allowed_action_bands_for_safety(safety_mode)
    if phase == "midday_review":
        if midday_decision and midday_decision in MIDDAY_DECISIONS:
            return [midday_decision]
        return allowed
    if phase in {"preopen_prepare", "postclose_archive"}:
        return allowed
    keep = [item for item in allowed if item in safety_allowed or item.endswith("_only") or item == "freeze"]
    return keep or list(safety_allowed)


def derive_formal_phase(
    cycle_state: Dict[str, Any],
    source_phase: str,
    market_stage: str,
) -> Tuple[str, str]:
    source = str(source_phase or cycle_state.get("current_phase", "") or "").strip()
    phase_bucket = dict(cycle_state.get("phases", {}) or {})
    status_map = {
        phase_name: str(dict(phase_bucket.get(phase_name, {}) or {}).get("status", "") or "")
        for phase_name in [
            "preopen_gate",
            "simulation",
            "shadow",
            "midday_review",
            "afternoon_execution",
            "afternoon_shadow",
            "summary",
        ]
    }
    if source == "summary" or status_map.get("summary") == "success":
        current = "postclose_archive"
    elif market_stage == "post_close" and status_map.get("midday_review") in FINAL_PHASE_STATUSES:
        current = "close_reconcile"
    elif source in {"afternoon_execution", "afternoon_shadow"} or status_map.get("afternoon_execution") == "running" or status_map.get("afternoon_shadow") == "running":
        current = "afternoon_adjust"
    elif source == "midday_review" or status_map.get("midday_review") == "running":
        current = "midday_review"
    elif source in {"simulation", "shadow"} or status_map.get("simulation") in FINAL_PHASE_STATUSES or status_map.get("shadow") in FINAL_PHASE_STATUSES:
        current = "morning_observe"
    elif source == "preopen_gate" or status_map.get("preopen_gate") in FINAL_PHASE_STATUSES or market_stage in {"opening_auction", "pre_open_pause"}:
        current = "morning_probe"
    else:
        current = "preopen_prepare"
    previous_idx = max(FORMAL_PHASE_INDEX.get(current, 0) - 1, 0)
    previous = FORMAL_PHASES[previous_idx] if current != FORMAL_PHASES[0] else ""
    return current, previous
