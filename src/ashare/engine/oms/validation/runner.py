from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

from live_execution_bridge.models import AccountState, OrderIntent, Position, TargetPosition
from ..contracts import ACTUAL_STATE_FIELDS, INTENT_LEDGER_FIELDS, ORDER_LEDGER_FIELDS
from ..core import (
    build_actual_state_frame,
    build_desired_vs_actual_gap,
    build_intent_continuity,
    build_intent_plan,
    finalize_intent_ledger,
    write_json_artifact,
)
from ..paths import build_oms_paths


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _target(symbol: str, weight: float, desired_state: str = "hold", desired_action: str = "hold") -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        target_weight=weight,
        score=1.0,
        raw={"desired_state": desired_state, "desired_action": desired_action, "mechanism_primary": "validation"},
    )


def _account(nav: float, cash: float, positions: List[Tuple[str, int, float]]) -> AccountState:
    return AccountState(
        account_id="validation_account",
        cash=float(cash),
        nav_value=float(nav),
        positions=[Position(symbol=symbol, shares=shares, avg_cost=price, last_price=price, available_shares=shares) for symbol, shares, price in positions],
    )


def _base_overrides() -> Dict[str, Any]:
    return {
        "intent_controls": {"force_close_intents": [], "force_expire_intents": [], "operator_cancel_intents": [], "freeze_intent_continuation": []},
        "symbol_controls": {"freeze_new_entry_symbols": [], "freeze_build_symbols": [], "force_actual_state": {}, "reconcile_required_symbols": []},
        "session_controls": {"ignore_stale_unfinished_orders": [], "expire_orders": [], "force_resync": False, "force_reconcile_only": False, "rebuild_actual_state": False},
    }


def _base_cfg() -> Dict[str, Any]:
    return {"intent_expiry_days": 3}


def _intent_row(intent_id: str, release_id: str, symbol: str, action_type: str, status: str, latest_order_id: str = "", expires_at: str = "") -> Dict[str, Any]:
    now_text = _now_text()
    return {
        "intent_id": intent_id,
        "release_id": release_id,
        "origin_release_id": release_id,
        "symbol": symbol,
        "action_type": action_type,
        "desired_state": action_type,
        "actual_state": "watch",
        "target_weight_before": 0.0,
        "target_weight_after": 0.0,
        "delta_weight": 0.0,
        "delta_shares": 0,
        "priority": 0.0,
        "urgency": 0.0,
        "reason": "validation",
        "status": status,
        "dispatch_block_reason": "",
        "continuation_status": "",
        "continuation_count": 0,
        "continuation_note": "",
        "supersedes_intent_id": "",
        "replaced_by_intent_id": "",
        "manual_override_flag": False,
        "reconcile_required": False,
        "created_at": now_text,
        "updated_at": now_text,
        "expires_at": expires_at or (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
        "latest_order_id": latest_order_id,
        "latest_fill_id": "",
    }


def _order_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for col in ORDER_LEDGER_FIELDS:
        if col not in frame.columns:
            frame[col] = pd.NA
    return frame[ORDER_LEDGER_FIELDS].copy()


def _intent_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for col in INTENT_LEDGER_FIELDS:
        if col not in frame.columns:
            frame[col] = pd.NA
    return frame[INTENT_LEDGER_FIELDS].copy()


def _actual_from_gap(account_state: AccountState, target_positions: List[TargetPosition], price_map: Dict[str, float], unfinished_orders: List[Dict[str, Any]], release_id: str) -> pd.DataFrame:
    gap = build_desired_vs_actual_gap(
        account_state=account_state,
        target_positions=target_positions,
        price_map=price_map,
        unfinished_orders=unfinished_orders,
        release_id=release_id,
        lot_size=100,
        cash_reserve_ratio=0.0,
    )
    return build_actual_state_frame(gap, intent_frame=pd.DataFrame(columns=INTENT_LEDGER_FIELDS))


def _scenario_match_full() -> Dict[str, Any]:
    symbol = "600000.SH"
    account = _account(nav=1000.0, cash=0.0, positions=[(symbol, 100, 10.0)])
    gap = build_desired_vs_actual_gap(account, [_target(symbol, 1.0, "hold", "hold")], {symbol: 10.0}, [], "r1", 100, 0.0)
    actual = build_actual_state_frame(gap, intent_frame=pd.DataFrame(columns=INTENT_LEDGER_FIELDS))
    state = str(actual.iloc[0]["actual_state"])
    return {"name": "synthetic_match_full", "passed": state == "hold" and int(gap.iloc[0]["gap_shares"]) == 0, "details": {"actual_state": state, "gap_shares": int(gap.iloc[0]["gap_shares"])}}


def _scenario_partial_residual_continuation() -> Dict[str, Any]:
    symbol = "600001.SH"
    account = _account(nav=3000.0, cash=2000.0, positions=[(symbol, 100, 10.0)])
    gap = build_desired_vs_actual_gap(account, [_target(symbol, 1.0, "build", "add")], {symbol: 10.0}, [], "r2", 100, 0.0)
    actual = build_actual_state_frame(gap, intent_frame=pd.DataFrame(columns=INTENT_LEDGER_FIELDS))
    prev = _intent_frame([_intent_row("intent_old", "r1", symbol, "add", "partial_fill")])
    continuity = build_intent_continuity(actual, prev, pd.DataFrame(columns=ORDER_LEDGER_FIELDS), [], "r2", _base_overrides(), _base_cfg())
    control = {"final_orders": [OrderIntent(symbol=symbol, side="BUY", target_shares=300, delta_shares=200, ref_price=10.0, reason="validation")]} 
    plan = build_intent_plan(actual, control, continuity["updated_previous_intents"], "r2", _base_overrides(), _base_cfg(), continuity=continuity)
    carried = continuity["carried_intent_by_symbol"].get(symbol)
    dispatched = list(plan.get("dispatch_orders", []) or [])
    same_intent = bool(plan["intent_frame"].iloc[0]["intent_id"] == "intent_old")
    return {"name": "partial_residual_continuation", "passed": bool(carried) and len(dispatched) == 1 and same_intent, "details": {"carried": bool(carried), "dispatch_count": len(dispatched), "intent_id": str(plan["intent_frame"].iloc[0]["intent_id"])}}


def _scenario_partial_exit_open_order() -> Dict[str, Any]:
    symbol = "600002.SH"
    account = _account(nav=3000.0, cash=0.0, positions=[(symbol, 300, 10.0)])
    gap = build_desired_vs_actual_gap(account, [], {symbol: 10.0}, [{"symbol": symbol, "order_id": "ord1", "cl_ord_id": "cl1", "side": "SELL", "remaining_shares": 200}], "r3", 100, 0.0)
    prev = _intent_frame([_intent_row("intent_sell", "r2", symbol, "exit", "acknowledged", latest_order_id="ord1")])
    actual = build_actual_state_frame(gap, intent_frame=prev)
    continuity = build_intent_continuity(actual, prev, pd.DataFrame(columns=ORDER_LEDGER_FIELDS), [{"symbol": symbol, "order_id": "ord1", "cl_ord_id": "cl1", "side": "SELL", "remaining_shares": 200}], "r3", _base_overrides(), _base_cfg())
    carried = continuity["carried_intent_by_symbol"].get(symbol, {})
    return {"name": "partial_exit_open_order", "passed": str(carried.get("continuity_status", "")) == "continue_with_open_order" and bool(carried.get("block_dispatch", False)), "details": carried}


def _scenario_stale_expiry() -> Dict[str, Any]:
    symbol = "600003.SH"
    account = _account(nav=1000.0, cash=1000.0, positions=[])
    gap = build_desired_vs_actual_gap(account, [_target(symbol, 1.0, "pilot", "new")], {symbol: 10.0}, [], "r4", 100, 0.0)
    expired_at = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    prev = _intent_frame([_intent_row("intent_expire", "r3", symbol, "new", "planned", expires_at=expired_at)])
    actual = build_actual_state_frame(gap, intent_frame=prev)
    continuity = build_intent_continuity(actual, prev, pd.DataFrame(columns=ORDER_LEDGER_FIELDS), [], "r4", _base_overrides(), _base_cfg())
    control = {"final_orders": [OrderIntent(symbol=symbol, side="BUY", target_shares=100, delta_shares=100, ref_price=10.0, reason="validation")]} 
    plan = build_intent_plan(actual, control, continuity["updated_previous_intents"], "r4", _base_overrides(), _base_cfg(), continuity=continuity)
    prev_status = str(continuity["updated_previous_intents"].iloc[0]["status"])
    new_intent_id = str(plan["intent_frame"].iloc[0]["intent_id"])
    return {"name": "stale_intent_expiry", "passed": prev_status == "expired" and new_intent_id != "intent_expire", "details": {"previous_status": prev_status, "new_intent_id": new_intent_id}}


def _scenario_contradictory_replace() -> Dict[str, Any]:
    symbol = "600004.SH"
    account = _account(nav=2000.0, cash=0.0, positions=[(symbol, 200, 10.0)])
    gap = build_desired_vs_actual_gap(account, [], {symbol: 10.0}, [{"symbol": symbol, "order_id": "ord2", "cl_ord_id": "cl2", "side": "BUY", "remaining_shares": 100}], "r5", 100, 0.0)
    prev = _intent_frame([_intent_row("intent_buy", "r4", symbol, "new", "submitted", latest_order_id="ord2")])
    actual = build_actual_state_frame(gap, intent_frame=prev)
    continuity = build_intent_continuity(actual, prev, pd.DataFrame(columns=ORDER_LEDGER_FIELDS), [{"symbol": symbol, "order_id": "ord2", "cl_ord_id": "cl2", "side": "BUY", "remaining_shares": 100}], "r5", _base_overrides(), _base_cfg())
    control = {"final_orders": [OrderIntent(symbol=symbol, side="SELL", target_shares=0, delta_shares=200, ref_price=10.0, reason="validation")]} 
    plan = build_intent_plan(actual, control, continuity["updated_previous_intents"], "r5", _base_overrides(), _base_cfg(), continuity=continuity)
    replacement = list(plan.get("replacement_links", []) or [])
    return {"name": "contradictory_release_cancel_replace", "passed": len(list(continuity.get("cancel_requests", []) or [])) == 1 and len(replacement) == 1, "details": {"cancel_requests": continuity.get("cancel_requests", []), "replacement_links": replacement}}


def _scenario_actual_truth_beats_old_target() -> Dict[str, Any]:
    symbol = "600005.SH"
    account = _account(nav=5000.0, cash=0.0, positions=[(symbol, 500, 10.0)])
    gap = build_desired_vs_actual_gap(account, [], {symbol: 10.0}, [], "r6", 100, 0.0)
    actual = build_actual_state_frame(gap, intent_frame=pd.DataFrame(columns=INTENT_LEDGER_FIELDS))
    return {"name": "actual_truth_beats_old_target", "passed": int(gap.iloc[0]["actual_shares"]) == 500 and str(actual.iloc[0]["actual_state"]) in {"trim", "exit"}, "details": {"actual_shares": int(gap.iloc[0]["actual_shares"]), "actual_state": str(actual.iloc[0]["actual_state"])}}


def _scenario_state_derivation_matrix() -> Dict[str, Any]:
    rows = [
        {"symbol": "A", "desired_state": "pilot", "actual_shares": 0, "target_shares": 100, "gap_shares": 100, "open_buy_shares": 100, "open_sell_shares": 0, "actual_weight": 0.0, "target_weight": 0.1},
        {"symbol": "B", "desired_state": "build", "actual_shares": 100, "target_shares": 300, "gap_shares": 200, "open_buy_shares": 0, "open_sell_shares": 0, "actual_weight": 0.1, "target_weight": 0.3},
        {"symbol": "C", "desired_state": "hold", "actual_shares": 200, "target_shares": 200, "gap_shares": 0, "open_buy_shares": 0, "open_sell_shares": 0, "actual_weight": 0.2, "target_weight": 0.2},
        {"symbol": "D", "desired_state": "trim", "actual_shares": 300, "target_shares": 100, "gap_shares": -200, "open_buy_shares": 0, "open_sell_shares": 0, "actual_weight": 0.3, "target_weight": 0.1},
        {"symbol": "E", "desired_state": "exit", "actual_shares": 200, "target_shares": 0, "gap_shares": -200, "open_buy_shares": 0, "open_sell_shares": 100, "actual_weight": 0.2, "target_weight": 0.0},
    ]
    frame = pd.DataFrame(rows)
    for col in ["release_id", "available_shares", "gap_weight", "gap_weight_abs", "mechanism_primary"]:
        if col not in frame.columns:
            frame[col] = 0 if col != "mechanism_primary" else "validation"
    actual = build_actual_state_frame(frame, intent_frame=pd.DataFrame(columns=INTENT_LEDGER_FIELDS))
    states = actual.set_index("symbol")["actual_state"].to_dict()
    expected = {"A": "pilot", "B": "build", "C": "hold", "D": "trim", "E": "exit"}
    return {"name": "state_derivation_matrix", "passed": states == expected, "details": states}


def _scenario_intent_lifecycle_matrix() -> Dict[str, Any]:
    intent_rows = [
        _intent_row("i1", "r7", "A", "new", "planned"),
        _intent_row("i2", "r7", "B", "new", "planned"),
        _intent_row("i3", "r7", "C", "trim", "planned"),
        _intent_row("i4", "r7", "D", "new", "planned"),
    ]
    order_rows = [
        {"order_id": "o1", "intent_id": "i1", "release_id": "r7", "symbol": "A", "broker_order_id": "o1", "cl_ord_id": "cl1", "submit_time": _now_text(), "side": "BUY", "price_type": "limit", "qty": 100, "filled_qty": 100, "remaining_qty": 0, "submit_price": 10.0, "status": "filled", "status_reason": "", "cancel_requested": False, "cancel_requested_at": "", "cancel_reason": "", "cancel_result": "", "updated_at": _now_text()},
        {"order_id": "o2", "intent_id": "i2", "release_id": "r7", "symbol": "B", "broker_order_id": "o2", "cl_ord_id": "cl2", "submit_time": _now_text(), "side": "BUY", "price_type": "limit", "qty": 100, "filled_qty": 0, "remaining_qty": 100, "submit_price": 10.0, "status": "rejected", "status_reason": "", "cancel_requested": False, "cancel_requested_at": "", "cancel_reason": "", "cancel_result": "", "updated_at": _now_text()},
        {"order_id": "o3", "intent_id": "i3", "release_id": "r7", "symbol": "C", "broker_order_id": "o3", "cl_ord_id": "cl3", "submit_time": _now_text(), "side": "SELL", "price_type": "limit", "qty": 100, "filled_qty": 0, "remaining_qty": 100, "submit_price": 10.0, "status": "cancelled", "status_reason": "", "cancel_requested": True, "cancel_requested_at": _now_text(), "cancel_reason": "manual", "cancel_result": "", "updated_at": _now_text()},
        {"order_id": "o4", "intent_id": "i4", "release_id": "r7", "symbol": "D", "broker_order_id": "o4", "cl_ord_id": "cl4", "submit_time": _now_text(), "side": "BUY", "price_type": "limit", "qty": 100, "filled_qty": 40, "remaining_qty": 60, "submit_price": 10.0, "status": "expired", "status_reason": "", "cancel_requested": False, "cancel_requested_at": "", "cancel_reason": "", "cancel_result": "", "updated_at": _now_text()},
    ]
    final = finalize_intent_ledger(_intent_frame(intent_rows), _order_frame(order_rows), pd.DataFrame(columns=[]))
    statuses = final.set_index("intent_id")["status"].to_dict()
    expected = {"i1": "filled", "i2": "rejected", "i3": "cancelled", "i4": "expired"}
    return {"name": "intent_lifecycle_matrix", "passed": statuses == expected, "details": statuses}


def _scenario_recovery_replay() -> Dict[str, Any]:
    symbol = "600006.SH"
    account = _account(nav=4000.0, cash=3000.0, positions=[(symbol, 100, 10.0)])
    gap = build_desired_vs_actual_gap(account, [_target(symbol, 0.75, "build", "add")], {symbol: 10.0}, [], "r8", 100, 0.0)
    prev = _intent_frame([_intent_row("intent_resume", "r7", symbol, "add", "partial_fill")])
    actual = build_actual_state_frame(gap, intent_frame=prev)
    continuity = build_intent_continuity(actual, prev, pd.DataFrame(columns=ORDER_LEDGER_FIELDS), [], "r8", _base_overrides(), _base_cfg())
    open_intents = continuity["open_intents_frame"]
    summary = continuity["session_resume_audit"]["summary"]
    return {"name": "recovery_replay_resume", "passed": int(summary.get("n_open_intents_after", 0)) >= 1 and not open_intents.empty, "details": {"summary": summary, "open_count": int(len(open_intents.index))}}


def _run_scenarios() -> List[Dict[str, Any]]:
    scenarios = [
        _scenario_match_full,
        _scenario_partial_residual_continuation,
        _scenario_partial_exit_open_order,
        _scenario_stale_expiry,
        _scenario_contradictory_replace,
        _scenario_actual_truth_beats_old_target,
        _scenario_state_derivation_matrix,
        _scenario_intent_lifecycle_matrix,
        _scenario_recovery_replay,
    ]
    results: List[Dict[str, Any]] = []
    for fn in scenarios:
        try:
            results.append(fn())
        except Exception as exc:
            results.append({"name": fn.__name__, "passed": False, "details": {"error": repr(exc)}})
    return results


def run_oms_validation_suite(config: Dict[str, Any]) -> Dict[str, Any]:
    oms_paths = build_oms_paths(config)
    results = _run_scenarios()
    n_passed = int(sum(bool(item.get("passed", False)) for item in results))
    report = {
        "generated_at": _now_text(),
        "summary": {
            "n_scenarios": int(len(results)),
            "n_passed": n_passed,
            "n_failed": int(len(results) - n_passed),
        },
        "scenarios": results,
    }
    summary_lines = [
        "# OMS Validation Summary",
        "",
        f"- Generated At: {report['generated_at']}",
        f"- Scenarios: {report['summary']['n_scenarios']}",
        f"- Passed: {report['summary']['n_passed']}",
        f"- Failed: {report['summary']['n_failed']}",
        "",
    ]
    for item in results:
        marker = "PASS" if bool(item.get("passed", False)) else "FAIL"
        summary_lines.append(f"- [{marker}] {item.get('name')}")
    write_json_artifact(oms_paths["oms_validation_report"], report)
    Path(oms_paths["oms_validation_summary"]).write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    report["artifacts"] = {
        "report_path": str(oms_paths["oms_validation_report"]),
        "summary_path": str(oms_paths["oms_validation_summary"]),
    }
    return report
