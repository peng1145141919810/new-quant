from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _series(frame: pd.DataFrame, column: str, default: Any = 0.0) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame.index), index=frame.index)


def build_lifecycle_frame(
    candidate_df: pd.DataFrame,
    posture: Dict[str, Any],
    portfolio_limits: Dict[str, Any],
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    if candidate_df is None or candidate_df.empty:
        return pd.DataFrame()
    frame = candidate_df.copy().reset_index(drop=True)
    required_defaults = {
        "is_existing_position": False,
        "tech_allow_entry": False,
        "router_allow_entry": True,
        "current_weight_ref": 0.0,
        "previous_state": "",
        "portfolio_weight": 0.0,
        "base_portfolio_weight": 0.0,
    }
    for column, default in required_defaults.items():
        if column not in frame.columns:
            frame[column] = default
    n = max(len(frame.index), 1)
    frame["model_rank_score"] = [1.0 - (idx / max(n - 1, 1)) for idx in range(n)]
    router_max = max(pd.to_numeric(_series(frame, "router_final_score", 0.0), errors="coerce").fillna(0.0).max(), 1e-9)
    frame["router_score_norm"] = pd.to_numeric(_series(frame, "router_final_score", 0.0), errors="coerce").fillna(0.0) / router_max
    hold_proxy = pd.to_numeric(_series(frame, "tech_hold_health", 0.0), errors="coerce").fillna(0.0)
    trend_proxy = pd.to_numeric(_series(frame, "tech_trend_score", 0.0), errors="coerce").fillna(0.0)
    existing_mask = _series(frame, "is_existing_position", False).fillna(False).astype(bool)
    existing_bonus = existing_mask.map(lambda x: 0.08 if x else 0.0)
    frame["size_confidence"] = (
        frame["model_rank_score"] * 0.40
        + pd.to_numeric(_series(frame, "tech_final_score", 0.0), errors="coerce").fillna(0.0) * 0.30
        + frame["router_score_norm"] * 0.15
        + (hold_proxy.where(existing_mask, trend_proxy)) * 0.15
        + existing_bonus
    ).apply(_clip)

    crowding_strength = float(cfg.get("soft_crowding_penalty_strength", 0.08) or 0.08)
    frame["crowding_penalty"] = 0.0
    if bool(cfg.get("soft_crowding_penalty_enabled", True)) and "mechanism_primary" in frame.columns:
        mech = frame["mechanism_primary"].fillna("").astype(str).str.strip()
        counts = mech.value_counts().to_dict()
        total = float(max(len(frame.index), 1))
        frame["crowding_penalty"] = mech.map(
            lambda x: crowding_strength * max((counts.get(str(x), 0) / total) - 0.34, 0.0) if str(x) else 0.0
        )
    frame["admission_score"] = (
        frame["size_confidence"]
        - pd.to_numeric(frame["crowding_penalty"], errors="coerce").fillna(0.0)
        + _series(frame, "tech_allow_entry", False).fillna(False).astype(bool).map(lambda x: 0.04 if x else -0.04)
        + _series(frame, "router_allow_entry", True).fillna(True).astype(bool).map(lambda x: 0.02 if x else -0.06)
    ).apply(_clip)
    frame["retention_score"] = (
        frame["size_confidence"]
        + pd.to_numeric(_series(frame, "current_weight_ref", 0.0), errors="coerce").fillna(0.0).apply(lambda x: min(float(x), 0.08) / 0.08 * 0.15 if x > 0 else 0.0)
        + existing_mask.map(lambda x: 0.10 if x else 0.0)
    ).apply(_clip)

    pilot_cap = min(float(cfg.get("pilot_max_weight", 0.04) or 0.04), float(portfolio_limits.get("single_name_cap", 0.10) or 0.10))
    single_cap = float(portfolio_limits.get("single_name_cap", 0.10) or 0.10)
    build_speed = float(cfg.get("build_speed", 1.25) or 1.25)
    trim_speed = float(cfg.get("trim_speed", 0.72) or 0.72)
    rebalance_mode = str(posture.get("rebalance_mode", "neutral") or "neutral")

    states = []
    actions = []
    intents = []
    caps = []
    proposals = []
    build_speeds = []
    trim_speeds = []
    drop_reasons = []
    for _, row in frame.iterrows():
        existing = bool(row.get("is_existing_position", False))
        size_conf = float(row.get("size_confidence", 0.0) or 0.0)
        current_weight = float(row.get("current_weight_ref", 0.0) or 0.0)
        base_weight = float(row.get("portfolio_weight", row.get("base_portfolio_weight", 0.0)) or 0.0)
        tech_allow = bool(row.get("tech_allow_entry", False))
        tech_style = str(row.get("tech_entry_style", "") or "")
        hold_health = float(row.get("tech_hold_health", 0.0) or 0.0)
        gate_reason = str(row.get("tech_gate_reason", "") or "")
        prev_state = str(row.get("previous_state", "") or "")
        if existing:
            if size_conf < 0.18 and hold_health < 0.10:
                state = "exit"
                action = "sell_exit"
                intent = "exit_position"
                cap = 0.0
                proposal = 0.0
                drop_reason = "state_exit_very_weak"
            elif gate_reason in {"hold_health_weak", "trend_not_confirmed"} and size_conf < 0.48:
                state = "trim"
                action = "sell_partial"
                intent = "trim_existing"
                cap = min(current_weight * trim_speed, single_cap)
                proposal = max(min(cap, base_weight), 0.0)
                drop_reason = ""
            elif tech_allow and size_conf >= 0.66 and rebalance_mode not in {"defend", "reduce_only"}:
                state = "build"
                action = "buy_add"
                intent = "build_existing"
                cap = min(single_cap, max(current_weight * build_speed, 0.035 + size_conf * 0.05))
                proposal = min(cap, max(base_weight, current_weight * min(build_speed, 1.12)))
                drop_reason = ""
            else:
                state = "hold" if prev_state not in {"trim", "exit"} else prev_state
                action = "hold"
                intent = "hold_existing"
                cap = min(single_cap, max(current_weight * 1.12, 0.03 + size_conf * 0.04))
                proposal = min(cap, max(base_weight, current_weight))
                drop_reason = ""
        else:
            if rebalance_mode == "reduce_only" or float(posture.get("new_entry_budget", 0.0) or 0.0) <= 0:
                state = "watch"
                action = "watch"
                intent = "defer_entry"
                cap = 0.0
                proposal = 0.0
                drop_reason = "posture_blocks_new_entries"
            elif tech_allow and size_conf >= 0.64:
                state = "build"
                action = "buy_build"
                intent = "new_entry_build"
                cap = min(single_cap, max(pilot_cap * 1.8, 0.028 + size_conf * 0.055))
                proposal = min(cap, max(base_weight, 0.018 + size_conf * 0.03))
                drop_reason = ""
            elif tech_style in {"pilot", "wait"} or size_conf >= 0.42:
                state = "pilot"
                action = "buy_pilot"
                intent = "new_entry_pilot"
                cap = pilot_cap
                proposal = min(cap, max(base_weight, 0.012 + size_conf * 0.018))
                drop_reason = ""
            else:
                state = "watch"
                action = "watch"
                intent = "defer_entry"
                cap = 0.0
                proposal = 0.0
                drop_reason = "candidate_too_weak"
        states.append(state)
        actions.append(action)
        intents.append(intent)
        caps.append(round(float(cap), 6))
        proposals.append(round(float(proposal), 6))
        build_speeds.append(round(float(build_speed if state in {"build", "hold"} else 0.0), 4))
        trim_speeds.append(round(float(trim_speed if state in {"trim", "exit"} else 0.0), 4))
        drop_reasons.append(drop_reason)
    frame["current_state"] = states
    frame["desired_state"] = states
    frame["recommended_action"] = actions
    frame["desired_action"] = actions
    frame["position_action_intent"] = intents
    frame["target_weight_cap_v2a"] = caps
    frame["proposal_target_weight"] = proposals
    frame["build_speed"] = build_speeds
    frame["trim_speed"] = trim_speeds
    frame["drop_reason"] = drop_reasons
    frame["replacement_score"] = (
        frame["admission_score"] - frame["retention_score"].min()
    ).apply(lambda x: round(float(x), 6))
    return frame
