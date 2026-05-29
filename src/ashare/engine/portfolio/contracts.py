from __future__ import annotations


LIFECYCLE_STATES = ["watch", "pilot", "build", "hold", "trim", "exit"]

POSITION_LIFECYCLE_FIELDS = [
    "date",
    "symbol",
    "previous_state",
    "desired_state",
    "current_state",
    "desired_action",
    "recommended_action",
    "position_action_intent",
    "is_existing_position",
    "selected_for_target",
    "base_target_weight",
    "proposal_target_weight",
    "final_target_weight",
    "target_weight_cap_v2a",
    "current_weight_ref",
    "size_confidence",
    "build_speed",
    "trim_speed",
    "admission_score",
    "retention_score",
    "replacement_score",
    "crowding_penalty",
    "mechanism_primary",
    "tech_gate_reason",
    "tech_entry_style",
    "router_signal_state",
    "drop_reason",
]
