from __future__ import annotations

from typing import Any, Dict, List


INTRADAY_SAFETY_MODES = ("NORMAL", "CAUTION", "PANIC", "HALT")


def derive_intraday_safety_mode(safety_state: Dict[str, Any]) -> str:
    system_mode = str(safety_state.get("system_mode", "") or "").strip().upper()
    market_regime = str(safety_state.get("market_safety_regime", "") or "").strip().upper()
    if bool(safety_state.get("manual_halt", False)) or system_mode == "HALT":
        return "HALT"
    if market_regime == "PANIC" or bool(safety_state.get("effective_reduce_only", False)):
        return "PANIC"
    if market_regime in {"CAUTION", "DEGRADED"} or system_mode in {"DEGRADED", "CAUTION"}:
        return "CAUTION"
    return "NORMAL"


def allowed_action_bands_for_safety(safety_mode: str) -> List[str]:
    mode = str(safety_mode or "NORMAL").strip().upper() or "NORMAL"
    if mode == "HALT":
        return ["reconcile_only", "freeze", "archive_only"]
    if mode == "PANIC":
        return ["trim_watch", "exit_execute", "reconcile_only", "hold_manage", "freeze"]
    if mode == "CAUTION":
        return ["pilot_entry", "hold_manage", "trim_watch", "exit_execute", "reconcile_only", "freeze"]
    return ["pilot_entry", "build_entry", "hold_manage", "trim_watch", "exit_execute", "reconcile_only", "freeze"]
