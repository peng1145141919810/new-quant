from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .tactical_merge import merge_tactical_orders_into_control_result

import pandas as pd

from live_execution_bridge.dev_log_snapshot import update_codex_dev_log_portfolio_snapshot
from live_execution_bridge.models import AccountState, FillRecord
from live_execution_bridge.portfolio_control import (
    _build_position_state_payload,
    _pending_share_maps,
    build_execution_feedback,
    load_control_config,
    plan_portfolio_control,
    write_portfolio_control_artifacts,
)
from live_execution_bridge.utils import dump_json, now_str, write_dataframe
from ..config_utils import ensure_dir
from .audit import CONTROL_DAILY_FIELDS, MECHANISM_ROLLUP_FIELDS, build_feedback_buckets
from .config import load_oms_config
from .contracts import (
    ACCOUNT_LEDGER_FIELDS,
    ACTUAL_STATE_FIELDS,
    FILL_LEDGER_FIELDS,
    INTENT_LEDGER_FIELDS,
    OPEN_INTENT_STATUSES,
    ORDER_LEDGER_FIELDS,
    POSITION_LEDGER_FIELDS,
)
from .core import (
    append_actual_state_daily,
    append_frame_rows,
    apply_manual_overrides_to_actual_state,
    build_actual_state_frame,
    build_actual_state_payload,
    build_desired_vs_actual_gap,
    build_intent_continuity,
    build_intent_plan,
    build_manual_override_summary,
    ensure_manual_overrides,
    filter_unfinished_orders_by_override,
    finalize_intent_ledger,
    load_ledger_frame,
    merge_fill_ledger,
    merge_order_ledger,
    record_manual_intervention_state,
    write_json_artifact,
    write_latest_ledger,
)
from .paths import build_oms_paths
from ..sql_store import load_runtime_json_artifact, resolve_sqlite_path, sql_store_enabled, sqlite_connection


def _account_payload(account_state: AccountState, broker_health: Dict[str, Any], release_id: str, account_mode: str) -> Dict[str, Any]:
    cash = float(account_state.cash)
    nav = float(account_state.nav())
    return {
        "snapshot_time": now_str(),
        "account_id": str(account_state.account_id),
        "total_asset": nav,
        "cash": cash,
        "available_cash": cash,
        "frozen_cash": max(nav - cash - sum(pos.market_value() for pos in account_state.positions), 0.0),
        "broker_health": broker_health.get("summary", {}),
        "snapshot_health": "ok",
        "account_mode": str(account_mode or ""),
        "source": "gmtrade_broker_truth",
        "release_id": str(release_id or ""),
    }


def _position_frame(account_state: AccountState, release_id: str) -> pd.DataFrame:
    nav = max(float(account_state.nav()), 1e-9)
    rows: List[Dict[str, Any]] = []
    for pos in account_state.positions:
        market_value = float(pos.market_value())
        rows.append(
            {
                "snapshot_time": now_str(),
                "account_id": str(account_state.account_id),
                "symbol": str(pos.symbol),
                "actual_shares": int(pos.shares),
                "available_shares": int(pos.available_shares),
                "cost_basis": float(pos.avg_cost),
                "market_value": market_value,
                "actual_weight": round(market_value / nav, 6),
                "realized_pnl": 0.0,
                "unrealized_pnl": round((float(pos.last_price) - float(pos.avg_cost)) * float(pos.shares), 4),
                "last_price": float(pos.last_price),
                "price_timestamp": now_str(),
                "price_source": "broker_position",
                "release_id": str(release_id or ""),
            }
        )
    frame = pd.DataFrame(rows)
    for col in POSITION_LEDGER_FIELDS:
        if col not in frame.columns:
            frame[col] = pd.NA
    return frame[POSITION_LEDGER_FIELDS].copy()


def _load_latest_release_id(config: Dict[str, Any]) -> str:
    release = dict(config.get("release", {}) or {})
    return str(release.get("release_id", "") or "")


def load_latest_oms_actual_state(config: Dict[str, Any]) -> Dict[str, Any]:
    path = build_oms_paths(config)["latest_actual_portfolio_state"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if payload:
                    return payload
            except Exception:
                pass
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_latest_oms_control_feedback(config: Dict[str, Any]) -> Dict[str, Any]:
    path = build_oms_paths(config)["control_feedback_latest"]
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if payload:
                    return payload
            except Exception:
                pass
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _apply_replacement_links(intent_frame: pd.DataFrame, replacement_links: List[Dict[str, Any]]) -> pd.DataFrame:
    frame = intent_frame.copy() if intent_frame is not None else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    if frame.empty or not replacement_links:
        return frame
    for link in list(replacement_links or []):
        old_intent_id = str(link.get("old_intent_id", "") or "")
        new_intent_id = str(link.get("new_intent_id", "") or "")
        if not old_intent_id or not new_intent_id:
            continue
        mask = frame["intent_id"].astype(str) == old_intent_id
        if mask.any():
            frame.loc[mask, "replaced_by_intent_id"] = new_intent_id
            if not frame.loc[mask, "status"].astype(str).isin(["filled", "cancelled", "rejected", "expired", "operator_cancelled", "operator_closed"]).all():
                frame.loc[mask, "status"] = "superseded"
    return frame


def _open_intents_payload(intent_ledger: pd.DataFrame) -> Dict[str, Any]:
    frame = intent_ledger.copy() if intent_ledger is not None else pd.DataFrame(columns=INTENT_LEDGER_FIELDS)
    if frame.empty:
        return {"generated_at": now_str(), "summary": {"n_open_intents": 0}, "intents": []}
    open_frame = frame.loc[frame["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy()
    open_frame = open_frame.where(pd.notna(open_frame), None)
    records = []
    for row in open_frame.to_dict(orient="records") if not open_frame.empty else []:
        clean = {}
        for key, value in row.items():
            if isinstance(value, str) and value.strip().lower() == "nan":
                clean[key] = None
            else:
                clean[key] = value
        records.append(clean)
    return {
        "generated_at": now_str(),
        "summary": {
            "n_open_intents": int(len(open_frame.index)),
            "status_counts": open_frame["status"].astype(str).value_counts().to_dict() if not open_frame.empty else {},
        },
        "intents": records,
    }


def run_oms_cycle(config: Dict[str, Any]) -> Dict[str, Any]:
    loader = globals().get("load_execution_snapshots")
    if not callable(loader):
        from .core.snapshot_loader import load_execution_snapshots as loader

    output_dir = ensure_dir(Path(str(config["output_dir"])))
    timestamp = now_str()
    release_context = dict(config.get("release", {}) or {})
    release_id = _load_latest_release_id(config)
    execution_policy = dict(config.get("execution_policy", {}) or {})
    execution_namespace = str(execution_policy.get("namespace", "main") or "main").strip() or "main"
    shadow_run = bool(execution_policy.get("shadow_run", False))
    oms_cfg = load_oms_config(config)
    oms_paths = build_oms_paths(config)
    load_ledger_frame._active_config = config
    write_latest_ledger._active_config = config
    append_actual_state_daily._active_config = config
    append_frame_rows._active_config = config
    write_json_artifact._active_config = config
    ensure_manual_overrides._active_config = config
    record_manual_intervention_state._active_config = config
    overrides = ensure_manual_overrides(oms_paths["manual_overrides"])
    manual_override_summary = build_manual_override_summary(overrides=overrides)

    snapshots = loader(config)
    broker = snapshots["broker"]
    portfolio_path = snapshots["portfolio_path"]
    target_positions = snapshots["target_positions"]
    price_map = snapshots["price_map"]
    before_state = snapshots["account_state"]
    broker_health_before = snapshots["order_health"]
    effective_unfinished_before, ignored_unfinished_before = filter_unfinished_orders_by_override(
        unfinished_orders=list(broker_health_before.get("unfinished_orders", []) or []),
        overrides=overrides,
    )

    control_cfg = load_control_config(config)
    preferred_t_mechanism = str(release_context.get("preferred_t_mechanism", "") or "").strip()
    if preferred_t_mechanism:
        control_cfg["preferred_t_mechanism"] = preferred_t_mechanism
        control_cfg["preferred_t_mechanism_enforced"] = True
        control_cfg["preferred_t_mechanism_buy_only"] = True
    broker_cfg = dict(config.get("broker", {}) or {})
    control_result = plan_portfolio_control(
        account_state=before_state,
        target_positions=target_positions,
        price_map=price_map,
        lot_size=int(broker_cfg.get("lot_size", 100)),
        min_trade_value=float(broker_cfg.get("min_trade_value", 2000.0)),
        cash_reserve_ratio=float(broker_cfg.get("cash_reserve_ratio", 0.02)),
        sell_by_available=bool(broker_cfg.get("sell_by_available", True)),
        control_cfg=control_cfg,
    )
    tactical_audit: Dict[str, Any] = {}
    tac_path = str(config.get("intraday_tactical_orders_path", "") or "").strip()
    if tac_path:
        control_result, tactical_audit = merge_tactical_orders_into_control_result(
            control_result,
            Path(tac_path),
            before_state,
            price_map,
        )

    force_resync = bool(dict(overrides.get("session_controls", {}) or {}).get("force_resync", False))
    prev_intent = pd.DataFrame(columns=INTENT_LEDGER_FIELDS) if force_resync else load_ledger_frame(oms_paths["intent_ledger_latest"], INTENT_LEDGER_FIELDS)
    prev_order = pd.DataFrame(columns=ORDER_LEDGER_FIELDS) if force_resync else load_ledger_frame(oms_paths["order_ledger_latest"], ORDER_LEDGER_FIELDS)
    prev_fill = pd.DataFrame(columns=FILL_LEDGER_FIELDS) if force_resync else load_ledger_frame(oms_paths["fill_ledger_latest"], FILL_LEDGER_FIELDS)
    actual_history_before = load_ledger_frame(oms_paths["actual_state_daily"], ACTUAL_STATE_FIELDS)

    gap_before = build_desired_vs_actual_gap(
        account_state=before_state,
        target_positions=target_positions,
        price_map=price_map,
        unfinished_orders=effective_unfinished_before,
        release_id=release_id,
        lot_size=int(broker_cfg.get("lot_size", 100)),
        cash_reserve_ratio=float(broker_cfg.get("cash_reserve_ratio", 0.02)),
    )
    actual_before_raw = build_actual_state_frame(gap_before, intent_frame=prev_intent)
    continuity = build_intent_continuity(
        actual_state_frame=actual_before_raw,
        previous_intent_frame=prev_intent,
        previous_order_frame=prev_order,
        unfinished_orders=effective_unfinished_before,
        release_id=release_id,
        overrides=overrides,
        oms_cfg=oms_cfg,
    )
    actual_before, manual_actual_override_rows = apply_manual_overrides_to_actual_state(
        actual_state_frame=actual_before_raw,
        overrides=overrides,
    )
    intent_plan = build_intent_plan(
        actual_state_frame=actual_before,
        control_result=control_result,
        previous_intent_frame=continuity["updated_previous_intents"],
        release_id=release_id,
        overrides=overrides,
        oms_cfg=oms_cfg,
        continuity=continuity,
    )
    dispatch_orders = list(intent_plan["dispatch_orders"])
    cancel_requests = list(continuity.get("cancel_requests", []) or [])
    cancel_results: List[Dict[str, Any]] = []
    if cancel_requests and (not shadow_run) and bool(oms_cfg.get("enable_broker_cancel", True)) and hasattr(broker, "cancel_orders"):
        cancel_results = list(broker.cancel_orders(cancel_requests) or [])

    raw_rows: List[Dict[str, Any]] = []
    fills: List[FillRecord] = []
    after_state = before_state
    broker_context: Dict[str, Any] = {"submitted_orders": [], "day_orders": [], "unfinished_orders": [], "cancel_results": cancel_results}
    if dispatch_orders and not shadow_run:
        after_state, fills, raw_rows, broker_context = broker.execute_orders(dispatch_orders, price_map)
        broker_context["cancel_results"] = cancel_results

    broker_health_after = broker.load_order_health()
    effective_unfinished_after, ignored_unfinished_after = filter_unfinished_orders_by_override(
        unfinished_orders=list(broker_health_after.get("unfinished_orders", []) or []),
        overrides=overrides,
    )
    fill_rows_after = broker.load_fill_rows()
    current_fill_rows = list(fill_rows_after or [])
    if not current_fill_rows and fills:
        current_fill_rows = [
            {
                "fill_id": str(item.exec_id or ""),
                "order_id": str(item.order_id or ""),
                "symbol": str(item.symbol),
                "side": str(item.side),
                "filled_qty": int(item.shares),
                "filled_price": float(item.price),
                "filled_amount": float(item.gross_amount),
                "fee": float(item.fee),
                "filled_time": now_str(),
            }
            for item in fills
        ]

    order_ledger = merge_order_ledger(
        previous_order_frame=prev_order,
        submitted_orders=list(broker_context.get("submitted_orders", []) or []),
        day_orders=list(broker_health_after.get("day_orders", []) or []),
        unfinished_orders=effective_unfinished_after,
        order_to_intent=dict(intent_plan.get("order_to_intent", {}) or {}),
        release_id=release_id,
        overrides=overrides,
        cancel_requests=cancel_requests,
        cancel_results=cancel_results,
    )
    fill_ledger = merge_fill_ledger(
        previous_fill_frame=prev_fill,
        fill_rows=current_fill_rows,
        order_frame=order_ledger,
    )
    merged_intent_seed = pd.concat([continuity["updated_previous_intents"], intent_plan["intent_frame"]], ignore_index=True)
    merged_intent_seed = _apply_replacement_links(merged_intent_seed, list(intent_plan.get("replacement_links", []) or []))
    intent_ledger = finalize_intent_ledger(
        intent_frame=merged_intent_seed,
        order_frame=order_ledger,
        fill_frame=fill_ledger,
    )
    if not intent_ledger.empty:
        intent_ledger = intent_ledger.drop_duplicates(subset=["intent_id"], keep="last").copy()

    gap_after = build_desired_vs_actual_gap(
        account_state=after_state,
        target_positions=target_positions,
        price_map=price_map,
        unfinished_orders=effective_unfinished_after,
        release_id=release_id,
        lot_size=int(broker_cfg.get("lot_size", 100)),
        cash_reserve_ratio=float(broker_cfg.get("cash_reserve_ratio", 0.02)),
    )
    actual_after_raw = build_actual_state_frame(gap_after, intent_frame=intent_ledger)
    actual_after, manual_actual_override_rows_after = apply_manual_overrides_to_actual_state(
        actual_state_frame=actual_after_raw,
        overrides=overrides,
    )
    account_payload = _account_payload(
        account_state=after_state,
        broker_health=broker_health_after,
        release_id=release_id,
        account_mode=str(execution_policy.get("account_mode", "") or ""),
    )
    actual_payload = build_actual_state_payload(
        account_payload=account_payload,
        actual_state_frame=actual_after,
        release_id=release_id,
    )

    account_ledger = pd.DataFrame([account_payload])
    position_ledger = _position_frame(after_state, release_id=release_id)
    for frame, fields in [
        (account_ledger, ACCOUNT_LEDGER_FIELDS),
        (position_ledger, POSITION_LEDGER_FIELDS),
        (intent_ledger, INTENT_LEDGER_FIELDS),
        (order_ledger, ORDER_LEDGER_FIELDS),
        (fill_ledger, FILL_LEDGER_FIELDS),
    ]:
        for col in fields:
            if col not in frame.columns:
                frame[col] = pd.NA

    write_latest_ledger(oms_paths["account_ledger_latest"], account_ledger, ACCOUNT_LEDGER_FIELDS, key_cols=["account_id"])
    write_latest_ledger(oms_paths["position_ledger_latest"], position_ledger, POSITION_LEDGER_FIELDS, key_cols=["account_id", "symbol"])
    write_latest_ledger(oms_paths["intent_ledger_latest"], intent_ledger, INTENT_LEDGER_FIELDS, key_cols=["intent_id"])
    write_latest_ledger(oms_paths["order_ledger_latest"], order_ledger, ORDER_LEDGER_FIELDS, key_cols=["order_id"])
    write_latest_ledger(oms_paths["fill_ledger_latest"], fill_ledger, FILL_LEDGER_FIELDS, key_cols=["fill_id"])
    gap_after.to_csv(oms_paths["desired_vs_actual_gap"], index=False, encoding="utf-8-sig")
    write_json_artifact(oms_paths["latest_actual_portfolio_state"], actual_payload)
    append_actual_state_daily(oms_paths["actual_state_daily"], actual_after, ACTUAL_STATE_FIELDS)

    execution_feedback = build_execution_feedback(
        planned_orders=dispatch_orders,
        skipped_actions=list(control_result.get("rebalance_audit", {}).get("turnover_adjustments", []) or []) + list(intent_plan.get("dispatch_blocked", []) or []),
        fills=fills,
        submitted_orders=list(broker_context.get("submitted_orders", []) or []),
        day_orders=list(broker_health_after.get("day_orders", []) or []),
        unfinished_orders=effective_unfinished_after,
    )
    pending_buy_map, pending_sell_map = _pending_share_maps(effective_unfinished_after)
    position_state_after_execution = _build_position_state_payload(
        stage="after_execution",
        account_state=after_state,
        target_weight_map=dict(control_result.get("target_weight_map", {}) or {}),
        raw_target_shares_map=dict(control_result.get("raw_target_shares_map", {}) or {}),
        effective_target_shares_map=dict(control_result.get("effective_target_shares_map", {}) or {}),
        price_map=price_map,
        control_cfg=control_cfg,
        pending_buy_map=pending_buy_map,
        pending_sell_map=pending_sell_map,
        target_meta_map=dict(control_result.get("target_meta_map", {}) or {}),
    )
    artifact_paths = write_portfolio_control_artifacts(
        output_dir=output_dir,
        timestamp=timestamp,
        position_state_before=dict(control_result["position_state_before"]),
        position_state_after_plan=dict(control_result["position_state_after_plan"]),
        position_state_after_execution=position_state_after_execution,
        rebalance_audit=dict(control_result["rebalance_audit"]),
        execution_feedback=execution_feedback,
    )

    open_intents_payload = _open_intents_payload(intent_ledger=intent_ledger)
    continuity_report = dict(continuity.get("continuity_report", {}) or {})
    continuity_report["cancel_results"] = cancel_results
    continuity_report["replacement_links"] = list(intent_plan.get("replacement_links", []) or [])
    session_resume_audit = dict(continuity.get("session_resume_audit", {}) or {})
    session_resume_audit["ignored_unfinished_orders_before"] = ignored_unfinished_before
    session_resume_audit["ignored_unfinished_orders_after"] = ignored_unfinished_after
    cancel_replace_audit = {
        "generated_at": now_str(),
        "release_id": release_id,
        "cancel_requests": cancel_requests,
        "cancel_results": cancel_results,
        "replacement_links": list(intent_plan.get("replacement_links", []) or []),
    }
    write_json_artifact(oms_paths["latest_open_intents"], open_intents_payload)
    write_json_artifact(oms_paths["latest_intent_continuity_report"], continuity_report)
    write_json_artifact(oms_paths["session_resume_audit"], session_resume_audit)
    write_json_artifact(oms_paths["cancel_replace_audit"], cancel_replace_audit)
    manual_intervention_state = record_manual_intervention_state(
        latest_state_path=oms_paths["latest_manual_intervention_state"],
        history_path=oms_paths["manual_override_history"],
        overrides=overrides,
        applied_summary={
            "manual_actual_state_overrides": len(manual_actual_override_rows_after),
            "ignored_unfinished_orders": len(ignored_unfinished_before) + len(ignored_unfinished_after),
            "cancel_requests": len(cancel_requests),
            "force_reconcile_only": int(bool(continuity.get("global_reconcile_only", False))),
        },
    )

    n_raw_orders = int(control_result.get("rebalance_audit", {}).get("n_raw_orders", 0) or 0)
    n_final_orders = int(control_result.get("rebalance_audit", {}).get("n_final_orders", 0) or 0)
    oms_summary = {
        "generated_at": now_str(),
        "release_id": release_id,
        "account": {
            "account_id": str(after_state.account_id),
            "nav": float(after_state.nav()),
            "cash": float(after_state.cash),
            "n_positions": int(len(after_state.positions)),
        },
        "dispatch": {
            "n_dispatch_orders": len(dispatch_orders),
            "n_submitted_orders": len(list(broker_context.get("submitted_orders", []) or [])),
            "n_fills": len(fills),
            "n_cancel_requests": len(cancel_requests),
            "n_cancel_results": len(cancel_results),
            "turnover_truncation_ratio": round(max(n_raw_orders - n_final_orders, 0) / max(n_raw_orders, 1), 6),
            "shadow_run": shadow_run,
        },
        "gap": {
            "n_gap_symbols": int(len(gap_after.index)),
            "gap_weight_ratio": round(float(pd.to_numeric(gap_after.get("gap_weight_abs", 0.0), errors="coerce").fillna(0.0).sum()), 6) if not gap_after.empty else 0.0,
        },
        "actual_state_counts": actual_after["actual_state"].astype(str).value_counts().to_dict() if not actual_after.empty else {},
        "intent_status_counts": intent_ledger["status"].astype(str).value_counts().to_dict() if not intent_ledger.empty else {},
        "authoritative_truth": {
            "desired_state_owner": "portfolio_recommendation_v2a_release",
            "actual_state_owner": "engine.oms",
            "order_truth_owner": "engine.oms",
            "broker_truth_owner": "engine.oms",
        },
        "manual_intervention": {
            "summary": manual_override_summary,
            "state_path": str(oms_paths["latest_manual_intervention_state"]),
            "history_path": str(oms_paths["manual_override_history"]),
        },
        "execution_namespace": execution_namespace,
        "continuity": dict(continuity_report.get("summary", {}) or {}),
        "compatibility": {
            "latest_account_state_json": str(output_dir / "latest_account_state.json"),
            "portfolio_control_artifacts": artifact_paths,
        },
        "tactical_merge": tactical_audit,
    }
    write_json_artifact(oms_paths["oms_summary"], oms_summary)

    actual_state_history = actual_history_before.copy() if actual_history_before is not None else pd.DataFrame(columns=ACTUAL_STATE_FIELDS)
    if not actual_after.empty:
        actual_state_history = pd.concat([actual_state_history, actual_after], ignore_index=True) if not actual_state_history.empty else actual_after.copy()
        actual_state_history = actual_state_history.drop_duplicates(subset=["date", "symbol"], keep="last")
    feedback_buckets = build_feedback_buckets(
        oms_summary=oms_summary,
        actual_state_frame=actual_after,
        intent_frame=intent_ledger,
        order_frame=order_ledger,
        fill_frame=fill_ledger,
        actual_state_history_frame=actual_state_history,
        control_lookback_runs=int(oms_cfg.get("control_feedback_lookback_runs", 20) or 20),
        research_lookback_runs=int(oms_cfg.get("research_meta_lookback_runs", 60) or 60),
    )
    write_json_artifact(oms_paths["truth_feedback_latest"], feedback_buckets["truth"])
    write_json_artifact(oms_paths["control_feedback_latest"], feedback_buckets["control"])
    write_json_artifact(oms_paths["research_meta_feedback_latest"], feedback_buckets["research_meta"])
    write_json_artifact(oms_paths["narrative_feedback_latest"], feedback_buckets["narrative"])
    append_frame_rows(oms_paths["gap_control_metrics_daily"], feedback_buckets["control_daily"], CONTROL_DAILY_FIELDS)
    mechanism_rollup = feedback_buckets["mechanism_rollup"]
    for col in MECHANISM_ROLLUP_FIELDS:
        if col not in mechanism_rollup.columns:
            mechanism_rollup[col] = pd.NA
    mechanism_rollup[MECHANISM_ROLLUP_FIELDS].to_csv(oms_paths["mechanism_realism_rollup"], index=False, encoding="utf-8-sig")

    write_dataframe(output_dir / f"orders_{timestamp}.csv", [x.to_dict() for x in dispatch_orders])
    write_dataframe(output_dir / f"fills_{timestamp}.csv", [x.to_dict() for x in fills])
    write_dataframe(output_dir / f"gmtrade_raw_{timestamp}.csv", raw_rows)
    write_dataframe(output_dir / "latest_target_snapshot.csv", [x.to_dict() for x in target_positions])
    if bool(oms_cfg.get("compat_write_latest_account_state", True)):
        dump_json(output_dir / "latest_account_state.json", after_state.to_dict())

    equity_curve_path = output_dir / "equity_curve.csv"
    new_row = {
        "timestamp": timestamp,
        "cash": after_state.cash,
        "nav": after_state.nav(),
        "n_positions": len(after_state.positions),
        "portfolio_file": str(portfolio_path),
    }
    if equity_curve_path.exists():
        eq = pd.read_csv(equity_curve_path)
        eq = pd.concat([eq, pd.DataFrame([new_row])], ignore_index=True)
    else:
        eq = pd.DataFrame([new_row])
    eq.to_csv(equity_curve_path, index=False, encoding="utf-8-sig")

    report_status = "shadow_executed" if shadow_run else "executed"
    report = {
        "ok": True,
        "status": report_status,
        "timestamp": timestamp,
        "portfolio_path": str(portfolio_path),
        "price_snapshot_path": str(config.get("price_snapshot_path", "")),
        "broker_type": "gmtrade_sim",
        "execution_policy": execution_policy,
        "execution_namespace": execution_namespace,
        "shadow_run": shadow_run,
        "n_target_positions": len(target_positions),
        "n_orders": len(dispatch_orders),
        "n_fills": len(fills),
        "before_nav": before_state.nav(),
        "after_nav": after_state.nav(),
        "before_cash": before_state.cash,
        "after_cash": after_state.cash,
        "positions_after": [x.to_dict() for x in after_state.positions],
        "portfolio_control": {
            "run_dir": artifact_paths["run_dir"],
            "drift_threshold": float(control_cfg.get("drift_threshold", 0.0)),
            "max_daily_turnover_ratio": float(control_cfg.get("max_daily_turnover_ratio", 0.0)),
            "raw_turnover_ratio": float(control_result["summary"].get("raw_turnover_ratio", 0.0)),
            "final_turnover_ratio": float(control_result["summary"].get("final_turnover_ratio", 0.0)),
            "n_drift_skipped_symbols": int(control_result["summary"].get("n_drift_skipped_symbols", 0) or 0),
            "n_turnover_adjustments": int(control_result["summary"].get("n_turnover_adjustments", 0) or 0),
            "execution_feedback_summary": dict(execution_feedback.get("summary", {}) or {}),
            "artifacts": artifact_paths,
        },
        "oms": {
            "root": str(oms_paths["root"]),
            "summary_path": str(oms_paths["oms_summary"]),
            "actual_state_path": str(oms_paths["latest_actual_portfolio_state"]),
            "gap_path": str(oms_paths["desired_vs_actual_gap"]),
            "intent_ledger_path": str(oms_paths["intent_ledger_latest"]),
            "order_ledger_path": str(oms_paths["order_ledger_latest"]),
            "fill_ledger_path": str(oms_paths["fill_ledger_latest"]),
            "open_intents_path": str(oms_paths["latest_open_intents"]),
            "continuity_path": str(oms_paths["latest_intent_continuity_report"]),
            "cancel_replace_audit_path": str(oms_paths["cancel_replace_audit"]),
            "manual_intervention_state_path": str(oms_paths["latest_manual_intervention_state"]),
            "control_feedback_path": str(oms_paths["control_feedback_latest"]),
            "research_meta_feedback_path": str(oms_paths["research_meta_feedback_latest"]),
            "summary": oms_summary,
        },
        "release": release_context,
    }
    execution_report_path = output_dir / f"execution_report_{timestamp}.json"
    report["execution_report_path"] = str(execution_report_path)
    dump_json(execution_report_path, report)
    if bool(control_cfg.get("enable_dev_log_snapshot", True)):
        update_codex_dev_log_portfolio_snapshot(
            dev_log_path=str(control_cfg.get("codex_dev_log_path") or (Path(__file__).resolve().parents[4] / "CODEX_DEV_LOG.md")),
            execution_report=report,
            after_state=after_state.to_dict(),
            control_summary=dict(control_result.get("summary", {}) or {}),
            execution_feedback=execution_feedback,
            top_holdings=int(control_cfg.get("dev_log_top_holdings", 8) or 8),
        )
    return report
