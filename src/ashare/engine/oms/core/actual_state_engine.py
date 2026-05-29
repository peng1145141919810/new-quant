from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import pandas as pd

from ..contracts.actual_state_schema import ACTUAL_STATE_FIELDS


def build_actual_state_frame(
    gap_frame: pd.DataFrame,
    intent_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if gap_frame is None or gap_frame.empty:
        return pd.DataFrame(columns=ACTUAL_STATE_FIELDS)
    intents = intent_frame.copy() if intent_frame is not None else pd.DataFrame(columns=["symbol", "status"])
    latest_intent_status: Dict[str, str] = {}
    if not intents.empty and "symbol" in intents.columns and "status" in intents.columns:
        intents = intents.copy()
        intents["updated_at"] = intents.get("updated_at", "").astype(str)
        intents = intents.sort_values(["symbol", "updated_at"])
        latest_intent_status = {
            str(row["symbol"]): str(row["status"] or "")
            for _, row in intents.iterrows()
        }
    frame = gap_frame.copy()
    frame["intent_status"] = frame["symbol"].astype(str).map(lambda x: latest_intent_status.get(str(x), ""))
    actual_states = []
    reasons = []
    action_types = []
    for _, row in frame.iterrows():
        desired_state = str(row.get("desired_state", "") or "")
        actual_shares = int(row.get("actual_shares", 0) or 0)
        target_shares = int(row.get("target_shares", 0) or 0)
        gap_shares = int(row.get("gap_shares", 0) or 0)
        open_buy = int(row.get("open_buy_shares", 0) or 0)
        open_sell = int(row.get("open_sell_shares", 0) or 0)
        intent_status = str(row.get("intent_status", "") or "")
        if target_shares <= 0 and actual_shares <= 0 and open_buy <= 0 and open_sell <= 0:
            actual_state = "watch"
            reason = "no_actual_and_no_target"
            action_type = "watch"
        elif target_shares <= 0 and actual_shares > 0:
            actual_state = "exit" if (open_sell > 0 or desired_state == "exit") else "trim"
            reason = "target_zero_but_actual_remaining"
            action_type = "exit"
        elif actual_shares <= 0 and target_shares > 0:
            if open_buy > 0 or intent_status in {"submitted", "acknowledged", "partial_fill"}:
                actual_state = "pilot" if desired_state in {"pilot", "build"} else "watch"
                reason = "new_entry_pending_broker_confirmation"
            else:
                actual_state = "watch"
                reason = "desired_exists_but_no_actual_position"
            action_type = "new"
        elif gap_shares < 0 or desired_state == "trim" or open_sell > 0:
            actual_state = "trim"
            reason = "actual_above_desired_or_sell_pending"
            action_type = "trim"
        elif gap_shares > 0 or desired_state == "build" or open_buy > 0:
            if desired_state == "pilot":
                actual_state = "pilot"
                reason = "pilot_position_still_building"
                action_type = "new"
            else:
                actual_state = "build"
                reason = "actual_below_desired_or_buy_pending"
                action_type = "add" if actual_shares > 0 else "new"
        else:
            actual_state = "hold"
            reason = "actual_close_to_desired"
            action_type = "hold"
        actual_states.append(actual_state)
        reasons.append(reason)
        action_types.append(action_type)
    frame["actual_state"] = actual_states
    frame["raw_actual_state"] = actual_states
    frame["actual_state_override"] = ""
    frame["actual_state_source"] = "derived"
    frame["state_gap_reason"] = reasons
    frame["action_type"] = action_types
    frame["reconcile_required"] = False
    frame["manual_override_reason"] = ""
    frame["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for col in ACTUAL_STATE_FIELDS:
        if col not in frame.columns:
            frame[col] = pd.NA
    return frame[ACTUAL_STATE_FIELDS].copy()


def build_actual_state_payload(
    account_payload: Dict[str, Any],
    actual_state_frame: pd.DataFrame,
    release_id: str,
) -> Dict[str, Any]:
    frame = actual_state_frame.copy() if actual_state_frame is not None else pd.DataFrame()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "release_id": str(release_id or ""),
        "account": account_payload,
        "summary": {
            "n_symbols": int(len(frame.index)),
            "actual_state_counts": frame["actual_state"].astype(str).value_counts().to_dict() if not frame.empty else {},
        },
        "positions": frame.to_dict(orient="records") if not frame.empty else [],
    }
