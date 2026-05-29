from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .config_utils import ensure_dir, load_config
from .portfolio_release import load_latest_release, load_release_by_id
from .safety_guard import load_system_safety_state
from .t_audit import build_t_audit_pack
from .trading_clock import clock_now


OPEN_ORDER_STATUSES = {"submitted", "acknowledged", "partial_fill", "cancel_requested"}


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve())


def _oms_base_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("oms_output_root", "") or "")).resolve())


def _midday_review_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(_trade_clock_root(config) / "midday_review")


def _intraday_state_root(config: Dict[str, Any]) -> Path:
    default_root = _trade_clock_root(config) / "intraday_state"
    return ensure_dir(
        Path(
            str(
                dict(config.get("intraday_state_machine", {}) or {}).get("artifact_root", default_root)
                or default_root
            )
        ).resolve()
    )


def _t_audit_latest_path(config: Dict[str, Any]) -> Path:
    data_root = Path(str(config.get("paths", {}).get("data_root", Path(__file__).resolve().parents[3] / "data") or Path(__file__).resolve().parents[3] / "data")).resolve()
    audit_cfg = dict(config.get("t_audit", {}) or {})
    root = Path(str(audit_cfg.get("artifact_root", data_root / "audit_v1") or data_root / "audit_v1")).resolve()
    return root / "latest" / "latest_t_audit.json"


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_release(config: Dict[str, Any], release_id: str = "") -> Dict[str, Any]:
    if str(release_id or "").strip():
        return load_release_by_id(config=config, release_id=str(release_id).strip())
    return load_latest_release(config=config)


def _namespace_root(base_root: Path, namespace: str) -> Path:
    ns = str(namespace or "").strip()
    if not ns or ns == "main":
        return base_root
    return base_root / ns


def _namespace_entry(base_root: Path, namespace: str) -> Dict[str, Any]:
    root = _namespace_root(base_root, namespace)
    summary_path = root / "snapshots" / "oms_summary.json"
    order_ledger_path = root / "ledgers" / "order_ledger_latest.csv"
    summary = _load_json(summary_path) if summary_path.exists() else {}
    open_order_count = 0
    acknowledged_count = 0
    filled_count = 0
    if order_ledger_path.exists():
        try:
            frame = pd.read_csv(order_ledger_path)
        except Exception:
            frame = pd.DataFrame()
        if not frame.empty and "status" in frame.columns:
            status_series = frame["status"].astype(str).str.strip().str.lower()
            open_order_count = int(status_series.isin(OPEN_ORDER_STATUSES).sum())
            acknowledged_count = int((status_series == "acknowledged").sum())
            filled_count = int((status_series == "filled").sum())
    return {
        "namespace": str(namespace or "main"),
        "root": str(root),
        "summary_path": str(summary_path) if summary_path.exists() else "",
        "order_ledger_path": str(order_ledger_path) if order_ledger_path.exists() else "",
        "release_id": str(summary.get("release_id", "") or ""),
        "n_submitted_orders": int(summary.get("dispatch", {}).get("n_submitted_orders", 0) or 0),
        "n_dispatch_orders": int(summary.get("dispatch", {}).get("n_dispatch_orders", 0) or 0),
        "n_fills": int(summary.get("dispatch", {}).get("n_fills", 0) or 0),
        "gap_weight_ratio": float(summary.get("gap", {}).get("gap_weight_ratio", 0.0) or 0.0),
        "open_order_count": open_order_count,
        "acknowledged_count": acknowledged_count,
        "filled_count": filled_count,
    }


def _scan_namespaces(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    base_root = _oms_base_root(config)
    namespaces: List[str] = []
    if (base_root / "snapshots" / "oms_summary.json").exists():
        namespaces.append("main")
    for child in sorted(base_root.iterdir()) if base_root.exists() else []:
        if child.is_dir() and (child / "snapshots" / "oms_summary.json").exists():
            namespaces.append(child.name)
    return [_namespace_entry(base_root, namespace) for namespace in namespaces]


def _latest_intraday_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    root = _intraday_state_root(config) / "latest"
    control = _load_json(root / "intraday_control_summary.json")
    phase = _load_json(root / "intraday_phase_state.json")
    return {
        "control_summary": control,
        "phase_state": phase,
    }


def _select_namespace(
    entries: List[Dict[str, Any]],
    release_id: str,
    preferred_namespace: str,
    forbidden_prefixes: List[str] | None = None,
) -> Dict[str, Any]:
    blocked_prefixes = [str(item or "").strip().lower() for item in list(forbidden_prefixes or []) if str(item or "").strip()]
    release_rows = [row for row in entries if str(row.get("release_id", "") or "") == str(release_id or "")]
    if preferred_namespace:
        for row in release_rows:
            if str(row.get("namespace", "") or "") == preferred_namespace:
                return row
    filtered = []
    for row in release_rows:
        ns = str(row.get("namespace", "") or "").strip().lower()
        if any(ns.startswith(prefix) for prefix in blocked_prefixes):
            continue
        filtered.append(row)
    if not filtered:
        return {}
    return sorted(
        filtered,
        key=lambda row: (
            int(row.get("open_order_count", 0) or 0),
            int(row.get("n_submitted_orders", 0) or 0),
            float(row.get("gap_weight_ratio", 0.0) or 0.0),
        ),
        reverse=True,
    )[0]


def _real_execution_plan(config: Dict[str, Any], release_doc: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    scheduler = dict(config.get("trade_clock", {}).get("scheduler", {}) or {})
    release_id = str(release_doc.get("release_id", "") or "")
    preferred_namespace = str(scheduler.get("simulation_namespace", "simulation") or "simulation")
    active = _select_namespace(entries, release_id=release_id, preferred_namespace=preferred_namespace, forbidden_prefixes=["shadow"])
    if not active:
        active = {
            "namespace": preferred_namespace,
            "release_id": release_id,
            "n_submitted_orders": 0,
            "n_dispatch_orders": 0,
            "n_fills": 0,
            "gap_weight_ratio": 0.0,
            "open_order_count": 0,
            "acknowledged_count": 0,
            "filled_count": 0,
        }
    gap_weight_ratio = float(active.get("gap_weight_ratio", 0.0) or 0.0)
    open_order_count = int(active.get("open_order_count", 0) or 0)
    if open_order_count > 0:
        action = "carry_and_reconcile"
        should_run = True
        reason = "open_orders_present_reconcile_existing_namespace"
    elif gap_weight_ratio >= 0.01:
        action = "followup_execute"
        should_run = True
        reason = "residual_gap_after_morning_execution"
    else:
        action = "skip"
        should_run = False
        reason = "gap_small_and_no_open_orders"
    return {
        "action": action,
        "should_run": should_run,
        "reason": reason,
        "namespace": str(active.get("namespace", preferred_namespace) or preferred_namespace),
        "release_id": release_id,
        "open_order_count": open_order_count,
        "acknowledged_count": int(active.get("acknowledged_count", 0) or 0),
        "n_submitted_orders": int(active.get("n_submitted_orders", 0) or 0),
        "n_fills": int(active.get("n_fills", 0) or 0),
        "gap_weight_ratio": gap_weight_ratio,
        "allow_unfinished_orders_reconcile": open_order_count > 0,
        "ignore_market_panic_reduce_only": bool(scheduler.get("simulation_ignore_market_panic_reduce_only", True)),
        "execution_mode": str(scheduler.get("simulation_execution_mode", "precision") or "precision"),
        "precision_trade_enabled": bool(scheduler.get("simulation_precision_trade", True)),
    }


def _shadow_execution_plan(config: Dict[str, Any], release_doc: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    scheduler = dict(config.get("trade_clock", {}).get("scheduler", {}) or {})
    release_id = str(release_doc.get("release_id", "") or "")
    preferred_namespace = str(scheduler.get("shadow_namespace", "shadow") or "shadow")
    release_rows = [row for row in entries if str(row.get("release_id", "") or "") == release_id]
    shadow_rows = [
        row
        for row in release_rows
        if str(row.get("namespace", "") or "").strip().lower().startswith("shadow")
    ]
    active = {}
    for row in shadow_rows:
        if str(row.get("namespace", "") or "") == preferred_namespace:
            active = row
            break
    if not active and shadow_rows:
        active = shadow_rows[0]
    if not active:
        active = {
            "namespace": preferred_namespace,
            "release_id": release_id,
            "n_submitted_orders": 0,
            "n_dispatch_orders": 0,
            "n_fills": 0,
            "gap_weight_ratio": 0.0,
            "open_order_count": 0,
            "acknowledged_count": 0,
            "filled_count": 0,
        }
    open_order_count = int(active.get("open_order_count", 0) or 0)
    gap_weight_ratio = float(active.get("gap_weight_ratio", 0.0) or 0.0)
    return {
        "action": "shadow_followup",
        "should_run": bool(release_id),
        "reason": "shadow_namespace_followup",
        "namespace": str(active.get("namespace", preferred_namespace) or preferred_namespace),
        "release_id": release_id,
        "open_order_count": open_order_count,
        "n_submitted_orders": int(active.get("n_submitted_orders", 0) or 0),
        "n_fills": int(active.get("n_fills", 0) or 0),
        "gap_weight_ratio": gap_weight_ratio,
        "allow_unfinished_orders_reconcile": open_order_count > 0,
        "ignore_market_panic_reduce_only": bool(scheduler.get("shadow_ignore_market_panic_reduce_only", True)),
        "execution_mode": str(scheduler.get("shadow_execution_mode", "precision") or "precision"),
        "precision_trade_enabled": bool(scheduler.get("shadow_precision_trade", True)),
    }


def run_midday_review(config_path: Path, release_id: str = "") -> Dict[str, Any]:
    config = load_config(config_path)
    now = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    release_doc = _load_release(config=config, release_id=release_id)
    release_trade_date = str(release_doc.get("trade_date", "") or "")
    entries = _scan_namespaces(config)
    safety_state = load_system_safety_state(config)
    intraday_summary = _latest_intraday_summary(config)
    intraday_control = dict(intraday_summary.get("control_summary", {}) or {})
    intraday_phase = dict(intraday_summary.get("phase_state", {}) or {})
    t_audit = build_t_audit_pack(config=config, trade_date=release_trade_date or now.date().isoformat(), release_doc=release_doc)
    real_plan = _real_execution_plan(config=config, release_doc=release_doc, entries=entries)
    shadow_plan = _shadow_execution_plan(config=config, release_doc=release_doc, entries=entries)
    plan = {
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": release_trade_date or now.date().isoformat(),
        "status": "ok",
        "release": {
            "release_id": str(release_doc.get("release_id", "") or ""),
            "trade_date": release_trade_date,
            "profile": str(release_doc.get("profile", "") or ""),
            "source_mode": str(release_doc.get("source_mode", "") or ""),
            "manifest_path": str(release_doc.get("artifacts", {}).get("manifest_path", "") or ""),
            "target_positions_path": str(release_doc.get("artifacts", {}).get("target_positions_path", "") or ""),
        },
        "market": {
            "system_mode": str(safety_state.get("system_mode", "") or ""),
            "market_safety_regime": str(safety_state.get("market_safety_regime", "") or ""),
            "gate_reason": str(safety_state.get("gate_reason", "") or ""),
            "effective_reduce_only": bool(safety_state.get("effective_reduce_only", False)),
            "panic_reduce_only_ignored_default": bool(config.get("trade_clock", {}).get("scheduler", {}).get("simulation_ignore_market_panic_reduce_only", True)),
            "snapshot": dict(safety_state.get("market_snapshot", {}) or {}),
        },
        "namespace_scan": entries,
        "timing_overlay_summary": {
            "summary_source": "latest_intraday_control_summary_pre_midday",
            "timing_window": str(intraday_control.get("timing_window", "") or intraday_phase.get("timing_window", "") or ""),
            "projected_afternoon_window": str(
                intraday_control.get("projected_afternoon_window", "")
                or intraday_phase.get("projected_afternoon_window", "")
                or ""
            ),
            "timing_enabled_symbols": int(intraday_control.get("timing_enabled_symbols", 0) or 0),
            "buy_ready_count": int(intraday_control.get("buy_ready_count", 0) or 0),
            "sell_ready_count": int(intraday_control.get("sell_ready_count", 0) or 0),
            "t_eligible_symbols": int(intraday_control.get("t_eligible_symbols", 0) or 0),
            "t_triggered_symbols": int(intraday_control.get("t_triggered_symbols", 0) or 0),
            "afternoon_second_leg_candidates": int(intraday_control.get("afternoon_second_leg_candidates", 0) or 0),
            "timing_feature_quality": dict(intraday_control.get("timing_feature_quality", {}) or {}),
            "overlay_recommendation": dict(intraday_control.get("overlay_recommendation", {}) or {}),
        },
        "t_audit_summary": {
            "available": bool(t_audit.get("available", False)),
            "top_reject_reason": str(t_audit.get("top_reject_reason", "") or ""),
            "top_suited_mechanism": str(t_audit.get("top_suited_mechanism", "") or ""),
            "summary_lines": list(t_audit.get("summary_lines", []) or [])[:4],
            "policy_change_suggestions": list(t_audit.get("policy_change_suggestions", []) or [])[:4],
        },
        "real_execution": real_plan,
        "shadow_execution": shadow_plan,
        "operator_summary": {
            "morning_namespace_owner": str(real_plan.get("namespace", "") or ""),
            "morning_open_order_count": int(real_plan.get("open_order_count", 0) or 0),
            "morning_gap_weight_ratio": float(real_plan.get("gap_weight_ratio", 0.0) or 0.0),
            "afternoon_should_run": bool(real_plan.get("should_run", False)),
            "afternoon_reason": str(real_plan.get("reason", "") or ""),
            "timing_window": str(intraday_control.get("timing_window", "") or ""),
            "projected_afternoon_window": str(
                intraday_control.get("projected_afternoon_window", "")
                or intraday_phase.get("projected_afternoon_window", "")
                or ""
            ),
            "timing_enabled_symbols": int(intraday_control.get("timing_enabled_symbols", 0) or 0),
            "buy_ready_count": int(intraday_control.get("buy_ready_count", 0) or 0),
            "sell_ready_count": int(intraday_control.get("sell_ready_count", 0) or 0),
            "t_eligible_symbols": int(intraday_control.get("t_eligible_symbols", 0) or 0),
            "t_triggered_symbols": int(intraday_control.get("t_triggered_symbols", 0) or 0),
            "afternoon_second_leg_candidates": int(intraday_control.get("afternoon_second_leg_candidates", 0) or 0),
        },
    }
    review_root = ensure_dir(_midday_review_root(config) / str(plan["trade_date"]).replace("-", ""))
    latest_root = ensure_dir(_midday_review_root(config) / "latest")
    plan_path = _write_json(review_root / "midday_adjustment_plan.json", plan)
    _write_json(latest_root / "midday_adjustment_plan.json", plan)
    summary_lines = [
        f"generated_at={plan['generated_at']}",
        f"release_id={plan['release']['release_id']}",
        f"trade_date={plan['trade_date']}",
        f"real_namespace={real_plan['namespace']}",
        f"real_action={real_plan['action']}",
        f"real_should_run={real_plan['should_run']}",
        f"real_reason={real_plan['reason']}",
        f"open_orders={real_plan['open_order_count']}",
        f"gap_weight_ratio={real_plan['gap_weight_ratio']}",
        f"timing_window={plan['timing_overlay_summary']['timing_window']}",
        f"projected_afternoon_window={plan['timing_overlay_summary']['projected_afternoon_window']}",
        f"buy_ready_count={plan['timing_overlay_summary']['buy_ready_count']}",
        f"sell_ready_count={plan['timing_overlay_summary']['sell_ready_count']}",
        f"t_eligible_symbols={plan['timing_overlay_summary']['t_eligible_symbols']}",
        f"t_triggered_symbols={plan['timing_overlay_summary']['t_triggered_symbols']}",
        f"t_top_reject_reason={plan['t_audit_summary']['top_reject_reason']}",
        f"t_top_suited_mechanism={plan['t_audit_summary']['top_suited_mechanism']}",
        f"shadow_namespace={shadow_plan['namespace']}",
        f"shadow_should_run={shadow_plan['should_run']}",
    ]
    (review_root / "midday_adjustment_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    (latest_root / "midday_adjustment_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    plan["artifacts"] = {
        "plan_path": str(plan_path),
        "latest_plan_path": str(latest_root / "midday_adjustment_plan.json"),
        "summary_path": str(review_root / "midday_adjustment_summary.txt"),
    }
    _write_json(plan_path, plan)
    _write_json(latest_root / "midday_adjustment_plan.json", plan)
    return plan
