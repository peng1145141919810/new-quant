from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from ..clock_account_snapshot import load_clock_account_snapshot_file
from ..market_state.runtime import load_latest_market_state
from ..oms.paths import build_oms_paths
from ..oms.state_reader import load_latest_oms_actual_state
from ..portfolio_release import load_latest_release, load_release_by_id
from ..safety_guard import load_system_safety_state
from ..trading_clock import clock_now, market_stage
from .artifact_writer import write_intraday_artifacts
from .event_model import build_intraday_events
from .intent_state import derive_intent_state_rows
from .phase_state import derive_formal_phase, derive_midday_decision, phase_allowed_action_bands
from .safety_mapping import derive_intraday_safety_mode
from .symbol_state import derive_symbol_state_rows
from .timing_layer import build_timing_overlay_payload


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve()


def _read_json(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    fallback = dict(default or {})
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _phase_state_path(config: Dict[str, Any], trade_date: str) -> Path:
    return _trade_clock_root(config) / "phase_state" / f"{str(trade_date or '').replace('-', '')}.json"


def _load_cycle_state(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    path = _phase_state_path(config, trade_date)
    if path.exists():
        return _read_json(path, default={})
    return {}


def _resolve_release(config: Dict[str, Any], cycle_state: Dict[str, Any], trade_date: str, explicit_release_id: str = "") -> Dict[str, Any]:
    release_id = str(explicit_release_id or cycle_state.get("release_id", "") or "").strip()
    if release_id:
        try:
            release_doc = load_release_by_id(config=config, release_id=release_id)
            if str(release_doc.get("trade_date", "") or "") == str(trade_date):
                return release_doc
        except Exception:
            pass
    try:
        latest_release = load_latest_release(config=config)
        if str(latest_release.get("trade_date", "") or "") == str(trade_date):
            return latest_release
        return {}
    except Exception:
        return {}


def _load_target_frame(release_doc: Dict[str, Any]) -> pd.DataFrame:
    path = Path(str(release_doc.get("target_positions_path", "") or release_doc.get("artifacts", {}).get("target_positions_path", "") or "")).resolve()
    return _read_csv(path)


def _load_market_state(config: Dict[str, Any], release_doc: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(release_doc.get("market_state", {}) or {})
    if payload:
        return payload
    try:
        return dict(load_latest_market_state(config=config, allow_build=False) or {})
    except Exception:
        return {}


def _oms_paths_for_namespace(config: Dict[str, Any], namespace: str) -> Dict[str, Path]:
    namespace_name = str(namespace or "main").strip() or "main"
    namespace_config = deepcopy(config)
    base_output_root = Path(str(config.get("oms", {}).get("output_root", config.get("paths", {}).get("oms_output_root", "")) or "")).resolve()
    namespace_config.setdefault("oms", {})
    namespace_config["oms"] = dict(namespace_config.get("oms", {}) or {})
    namespace_config["oms"]["output_root"] = str(base_output_root if namespace_name == "main" else (base_output_root / namespace_name))
    return build_oms_paths(namespace_config)


def _load_midday_plan(config: Dict[str, Any]) -> Dict[str, Any]:
    latest_plan = _trade_clock_root(config) / "midday_review" / "latest" / "midday_adjustment_plan.json"
    return _read_json(latest_plan, default={})


def _select_namespace(config: Dict[str, Any], source_phase: str, midday_plan: Dict[str, Any], cycle_state: Dict[str, Any]) -> str:
    scheduler = dict(config.get("trade_clock", {}).get("scheduler", {}) or {})
    if str(source_phase or "").strip() == "afternoon_shadow":
        return str(midday_plan.get("shadow_execution", {}).get("namespace", "") or scheduler.get("shadow_namespace", "shadow") or "shadow")
    if str(source_phase or "").strip() in {"afternoon_execution", "midday_review"}:
        return str(midday_plan.get("real_execution", {}).get("namespace", "") or scheduler.get("simulation_namespace", "simulation") or "simulation")
    return str(cycle_state.get("intraday_namespace", "") or scheduler.get("simulation_namespace", "simulation") or "simulation")


def _phase_status_overview(cycle_state: Dict[str, Any]) -> Dict[str, str]:
    phase_bucket = dict(cycle_state.get("phases", {}) or {})
    return {key: str(dict(phase_bucket.get(key, {}) or {}).get("status", "") or "") for key in phase_bucket.keys()}


def _counts(frame: pd.DataFrame, column: str) -> Dict[str, int]:
    if frame is None or frame.empty or column not in frame.columns:
        return {}
    return {str(k): int(v) for k, v in frame[column].astype(str).value_counts().to_dict().items()}


def _effective_market_stage(now_dt: Any, source_phase: str) -> str:
    source = str(source_phase or "").strip()
    if source.startswith("intraday_tactical_"):
        if any(tag in source for tag in ("_1310", "_1350", "_1420")):
            return "afternoon_session"
        return "morning_session"
    override = {
        "preopen_gate": "pre_open",
        "simulation": "morning_session",
        "shadow": "morning_session",
        "midday_review": "midday_break",
        "afternoon_execution": "afternoon_session",
        "afternoon_shadow": "afternoon_session",
        "summary": "post_close",
    }.get(source, "")
    return override or market_stage(now_dt)


def build_intraday_state_snapshot(
    *,
    config: Dict[str, Any],
    trade_date: str = "",
    source_phase: str = "",
    cycle_state: Dict[str, Any] | None = None,
    now_dt: Any | None = None,
) -> Dict[str, Any]:
    intraday_cfg = dict(config.get("intraday_state_machine", {}) or {})
    shadow_mode = bool(intraday_cfg.get("shadow_mode", True))
    now_dt = now_dt or clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    resolved_trade_date = str(trade_date or now_dt.date().isoformat())
    cycle = dict(cycle_state or _load_cycle_state(config, resolved_trade_date) or {})
    release_doc = _resolve_release(config=config, cycle_state=cycle, trade_date=resolved_trade_date)
    if str(release_doc.get("trade_date", "") or "").strip():
        resolved_trade_date = str(trade_date or release_doc.get("trade_date", "") or resolved_trade_date)
        if not cycle:
            cycle = dict(_load_cycle_state(config, resolved_trade_date) or {})
            release_doc = _resolve_release(
                config=config,
                cycle_state=cycle,
                trade_date=resolved_trade_date,
                explicit_release_id=str(release_doc.get("release_id", "") or ""),
            )
    release_id = str(release_doc.get("release_id", "") or cycle.get("release_id", "") or "")
    midday_plan = _load_midday_plan(config)
    namespace = _select_namespace(config=config, source_phase=source_phase, midday_plan=midday_plan, cycle_state=cycle)
    oms_paths = _oms_paths_for_namespace(config, namespace)
    actual_payload = load_latest_oms_actual_state({**config, "oms": {**dict(config.get("oms", {}) or {}), "output_root": str(oms_paths["root"])}})
    actual_positions = pd.DataFrame(list(actual_payload.get("positions", []) or []))
    gap_frame = _read_csv(oms_paths["desired_vs_actual_gap"])
    intent_frame = _read_csv(oms_paths["intent_ledger_latest"])
    order_frame = _read_csv(oms_paths["order_ledger_latest"])
    fill_frame = _read_csv(oms_paths["fill_ledger_latest"])
    continuity_report = _read_json(oms_paths["latest_intent_continuity_report"], default={})
    cancel_replace_audit = _read_json(oms_paths["cancel_replace_audit"], default={})
    oms_summary = _read_json(oms_paths["oms_summary"], default={})
    safety_state = load_system_safety_state(config)
    target_frame = _load_target_frame(release_doc)
    safety_mode = derive_intraday_safety_mode(safety_state)
    market_state = _load_market_state(config=config, release_doc=release_doc)
    effective_market_stage = _effective_market_stage(now_dt, source_phase)
    current_phase, previous_phase = derive_formal_phase(cycle_state=cycle, source_phase=source_phase, market_stage=effective_market_stage)
    midday_decision = derive_midday_decision(midday_plan, safety_mode)
    intent_state_frame = derive_intent_state_rows(
        intent_frame=intent_frame,
        order_frame=order_frame,
        fill_frame=fill_frame,
        cancel_replace_audit=cancel_replace_audit,
        continuity_report=continuity_report,
        namespace=namespace,
        trade_date=resolved_trade_date,
        release_id=release_id,
        force_reconcile_only=current_phase in {"close_reconcile", "postclose_archive"} or str(safety_mode or "").upper() == "HALT",
        stale_minutes=int(dict(config.get("intraday_state_machine", {}) or {}).get("stale_order_minutes", 20) or 20),
        now_ts=now_dt.replace(tzinfo=None),
    )
    symbol_state_frame = derive_symbol_state_rows(
        target_frame=target_frame,
        actual_positions_frame=actual_positions,
        gap_frame=gap_frame,
        intent_state_frame=intent_state_frame,
        phase_name=current_phase,
        safety_mode=safety_mode,
        midday_decision=midday_decision,
        release_id=release_id,
        trade_date=resolved_trade_date,
    )
    timing_payload = build_timing_overlay_payload(
        config=config,
        trade_date=resolved_trade_date,
        now_dt=now_dt,
        current_phase=current_phase,
        symbol_state_frame=symbol_state_frame,
        target_frame=target_frame,
        actual_positions_frame=actual_positions,
        market_state=market_state,
        safety_mode=safety_mode,
    )
    if isinstance(timing_payload.get("symbol_state_frame"), pd.DataFrame) and not timing_payload["symbol_state_frame"].empty:
        symbol_state_frame = timing_payload["symbol_state_frame"]
    timing_summary = dict(timing_payload.get("timing_summary", {}) or {})
    current_window = dict(timing_payload.get("current_window", {}) or {})
    afternoon_projection = dict(timing_payload.get("afternoon_projection", {}) or {})
    allowed_actions = phase_allowed_action_bands(current_phase, safety_mode, midday_decision=midday_decision)
    phase_state = {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "trade_date": resolved_trade_date,
        "current_phase": current_phase,
        "previous_phase": previous_phase,
        "source_phase": str(source_phase or cycle.get("current_phase", "") or ""),
        "release_id": release_id,
        "namespace": namespace,
        "market_stage": effective_market_stage,
        "safety_mode": safety_mode,
        "system_mode": str(safety_state.get("system_mode", "") or ""),
        "market_safety_regime": str(safety_state.get("market_safety_regime", "") or ""),
        "midday_decision": midday_decision,
        "timing_window": str(current_window.get("name", "") or ""),
        "projected_afternoon_window": str(afternoon_projection.get("name", "") or ""),
        "allowed_action_bands": allowed_actions,
        "phase_status_overview": _phase_status_overview(cycle),
        "manual_halt": bool(safety_state.get("manual_halt", False)),
        "manual_reduce_only": bool(safety_state.get("manual_reduce_only", False)),
        "execution_timing_enabled": bool(dict(intraday_cfg.get("timing_layer", {}) or {}).get("enabled", True)),
        "t_overlay_enabled": bool(dict(intraday_cfg.get("t_overlay", {}) or {}).get("enabled", True)),
        "shadow_mode": shadow_mode,
        "integration_mode": "shadow" if shadow_mode else "bounded_takeover",
        "updated_at": now_dt.isoformat(timespec="seconds"),
    }
    events = build_intraday_events(
        trade_date=resolved_trade_date,
        release_id=release_id,
        namespace=namespace,
        phase_state=phase_state,
        safety_state=safety_state,
        intent_frame=intent_state_frame,
        symbol_frame=symbol_state_frame,
        now_ts=now_dt.replace(tzinfo=None),
    )
    control_summary = {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "trade_date": resolved_trade_date,
        "release_id": release_id,
        "namespace": namespace,
        "phase_trace": {
            "current_phase": current_phase,
            "previous_phase": previous_phase,
            "source_phase": str(source_phase or cycle.get("current_phase", "") or ""),
            "phase_status_overview": phase_state["phase_status_overview"],
        },
        "symbol_state_counts": _counts(symbol_state_frame, "symbol_state"),
        "timing_state_counts": _counts(symbol_state_frame, "timing_state"),
        "t_overlay_state_counts": _counts(symbol_state_frame, "t_overlay_state"),
        "intent_state_counts": _counts(intent_state_frame, "intent_state"),
        "freeze_count": int((symbol_state_frame.get("symbol_state", pd.Series(dtype=str)).astype(str) == "freeze").sum()) if not symbol_state_frame.empty else 0,
        "reconcile_only_count": int((symbol_state_frame.get("symbol_state", pd.Series(dtype=str)).astype(str) == "reconcile_only").sum()) if not symbol_state_frame.empty else 0,
        "replace_required_count": int((intent_state_frame.get("intent_state", pd.Series(dtype=str)).astype(str) == "replace_required").sum()) if not intent_state_frame.empty else 0,
        "cancel_requested_count": int((intent_state_frame.get("intent_state", pd.Series(dtype=str)).astype(str) == "cancel_requested").sum()) if not intent_state_frame.empty else 0,
        "timing_window": str(current_window.get("name", "") or ""),
        "projected_afternoon_window": str(afternoon_projection.get("name", "") or ""),
        "timing_enabled_symbols": int(timing_summary.get("timing_enabled_symbols", 0) or 0),
        "t_eligible_symbols": int(timing_summary.get("t_eligible_symbols", 0) or 0),
        "t_triggered_symbols": int(timing_summary.get("t_triggered_symbols", 0) or 0),
        "buy_window_open_count": int(timing_summary.get("buy_window_open_count", 0) or 0),
        "sell_window_open_count": int(timing_summary.get("sell_window_open_count", 0) or 0),
        "timing_frozen_count": int(timing_summary.get("timing_frozen_count", 0) or 0),
        "t_completed_count": int(timing_summary.get("t_completed_count", 0) or 0),
        "buy_ready_count": int(timing_summary.get("buy_ready_count", 0) or 0),
        "sell_ready_count": int(timing_summary.get("sell_ready_count", 0) or 0),
        "afternoon_second_leg_candidates": int(timing_summary.get("afternoon_second_leg_candidates", 0) or 0),
        "midday_action": midday_decision,
        "afternoon_execution_outcome": str(dict(cycle.get("phases", {}).get("afternoon_execution", {}) or {}).get("status", "") or ""),
        "close_reconcile_outcome": "archived" if current_phase == "postclose_archive" else ("active" if current_phase == "close_reconcile" else ""),
        "risk_summary": {
            "safety_mode": safety_mode,
            "system_mode": str(safety_state.get("system_mode", "") or ""),
            "market_safety_regime": str(safety_state.get("market_safety_regime", "") or ""),
            "halt_reason": str(safety_state.get("halt_reason", "") or ""),
            "effective_reduce_only": bool(safety_state.get("effective_reduce_only", False)),
            "open_intents_after": int(dict(continuity_report.get("summary", {}) or {}).get("n_open_intents_after", 0) or 0),
            "n_gap_symbols": int(dict(oms_summary.get("gap", {}) or {}).get("n_gap_symbols", 0) or (len(gap_frame.index) if not gap_frame.empty else 0)),
        },
        "overlay_recommendation": {
            "midday_action": midday_decision,
            "allow_unfinished_orders_reconcile": bool(midday_decision in {"carry_and_reconcile", "risk_reduce"}) or bool(dict(midday_plan.get("real_execution", {}) or {}).get("allow_unfinished_orders_reconcile", False)),
            "block_new_entries": bool(midday_decision in {"risk_reduce", "abort_new_entries"} or safety_mode == "HALT"),
            "force_reconcile_only": bool(current_phase in {"close_reconcile", "postclose_archive"} or safety_mode == "HALT"),
            "panic_degrade_only": bool(safety_mode == "PANIC"),
            "timing_window": str(current_window.get("name", "") or ""),
            "projected_afternoon_window": str(afternoon_projection.get("name", "") or ""),
            "timing_layer_active": bool(timing_summary.get("timing_enabled_symbols", 0) or 0),
            "buy_ready_count": int(timing_summary.get("buy_ready_count", 0) or 0),
            "sell_ready_count": int(timing_summary.get("sell_ready_count", 0) or 0),
            "afternoon_second_leg_candidates_count": int(timing_summary.get("afternoon_second_leg_candidates", 0) or 0),
            "t_triggered_count": int(timing_summary.get("t_triggered_symbols", 0) or 0),
            "block_new_t": bool(dict(timing_summary.get("overlay_recommendation", {}) or {}).get("block_new_t", False)),
        },
        "timing_feature_quality": dict(timing_summary.get("feature_quality_counts", {}) or {}),
        "shadow_mode": shadow_mode,
        "integration_mode": "shadow" if shadow_mode else "bounded_takeover",
        "paths": {
            "oms_root": str(oms_paths["root"]),
            "oms_summary": str(oms_paths["oms_summary"]),
            "gap_csv": str(oms_paths["desired_vs_actual_gap"]),
            "intent_ledger": str(oms_paths["intent_ledger_latest"]),
            "order_ledger": str(oms_paths["order_ledger_latest"]),
            "fill_ledger": str(oms_paths["fill_ledger_latest"]),
            "release_target_positions": str(release_doc.get("target_positions_path", "") or release_doc.get("artifacts", {}).get("target_positions_path", "") or ""),
            "phase_state_source": str(_phase_state_path(config, resolved_trade_date)),
        },
        "event_count": len(events),
    }
    clock_acct = load_clock_account_snapshot_file(config)
    if clock_acct:
        control_summary["clock_account_snapshot"] = clock_acct
        ob = control_summary["overlay_recommendation"]
        cr = str(clock_acct.get("concentration_risk") or "").strip().lower()
        if cr == "high":
            ob["block_new_t"] = bool(ob.get("block_new_t", False)) or True
    manifest = write_intraday_artifacts(
        config=config,
        trade_date=resolved_trade_date,
        phase_state=phase_state,
        symbol_state_frame=symbol_state_frame,
        intent_state_frame=intent_state_frame,
        event_rows=events,
        control_summary=control_summary,
    )
    return {
        "ok": True,
        "trade_date": resolved_trade_date,
        "release_id": release_id,
        "namespace": namespace,
        "phase_state": phase_state,
        "control_summary": control_summary,
        "manifest": manifest,
    }


def refresh_intraday_state_machine(
    *,
    config: Dict[str, Any],
    trade_date: str = "",
    source_phase: str = "",
    cycle_state: Dict[str, Any] | None = None,
    now_dt: Any | None = None,
) -> Dict[str, Any]:
    cfg = dict(config.get("intraday_state_machine", {}) or {})
    shadow_mode = bool(cfg.get("shadow_mode", True))
    if not bool(cfg.get("enabled", True)):
        return {"ran": False, "ok": True, "message": "intraday_state_machine_disabled"}
    try:
        snapshot = build_intraday_state_snapshot(config=config, trade_date=trade_date, source_phase=source_phase, cycle_state=cycle_state, now_dt=now_dt)
        phase_state = dict(snapshot.get("phase_state", {}) or {})
        return {
            "ran": True,
            "ok": True,
            "trade_date": str(snapshot.get("trade_date", "") or ""),
            "release_id": str(snapshot.get("release_id", "") or ""),
            "namespace": str(snapshot.get("namespace", "") or ""),
            "current_phase": str(phase_state.get("current_phase", "") or ""),
            "shadow_mode": shadow_mode,
            "integration_mode": "shadow" if shadow_mode else "bounded_takeover",
            "manifest": dict(snapshot.get("manifest", {}) or {}),
        }
    except Exception as exc:
        if bool(cfg.get("fail_open", True)):
            return {"ran": True, "ok": False, "message": str(exc), "fail_open": True}
        raise
