from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from ..t_audit import resolve_t_execution_policy


TIMING_STATES = [
    "timing_frozen",
    "reconcile_only",
    "observe",
    "buy_watch",
    "buy_ready",
    "sell_watch",
    "sell_ready",
    "dual_ready",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if pd.isna(out):
        return float(default)
    return float(out)


def apply_timing_rules(
    *,
    frame: pd.DataFrame,
    current_window: Dict[str, Any],
    safety_mode: str,
    config: Dict[str, Any],
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()

    intraday_cfg = dict(config.get("intraday_state_machine", {}) or {})
    timing_cfg = dict(intraday_cfg.get("timing_layer", {}) or {})
    buy_threshold = float(timing_cfg.get("buy_score_threshold", 0.58) or 0.58)
    sell_threshold = float(timing_cfg.get("sell_score_threshold", 0.62) or 0.62)
    require_oms_clean = bool(timing_cfg.get("require_oms_clean_state", True))
    require_flow_confirmation = bool(timing_cfg.get("require_flow_confirmation", True))
    timing_enabled = bool(timing_cfg.get("enabled", True))

    rows = []
    for _, series in frame.iterrows():
        row = dict(series.to_dict())
        lifecycle = str(row.get("source_lifecycle_state", "") or "").strip().lower()
        symbol_state = str(row.get("symbol_state", "") or "").strip().lower()
        buy_score = _to_float(row.get("buy_timing_score"), 0.0)
        sell_score = _to_float(row.get("sell_timing_score"), 0.0)
        flow_ok = bool(row.get("volume_confirmation_flag", False))
        message_veto = bool(row.get("message_veto_flag", False))
        low_liquidity = bool(row.get("low_liquidity_flag", False))
        snapshot_missing = str(row.get("feature_quality_tier", "") or "") == "no_live_snapshot"
        proxy_stale = str(row.get("feature_quality_tier", "") or "") == "proxy_stale"
        proxy_spread = _to_float(row.get("proxy_spread_pct"), 0.0)
        proxy_tick_imbalance = _to_float(row.get("proxy_tick_imbalance"), 0.0)
        proxy_top_list_hit = bool(row.get("proxy_top_list_hit", False))
        last_intent_state = str(row.get("last_intent_state", "") or "").strip().lower()
        oms_clean_state = last_intent_state not in {"reconcile_only", "stale_pending", "replace_required", "cancel_requested"}
        buy_window_open = bool(current_window.get("allow_new_entry", False) or current_window.get("allow_build_entry", False))
        sell_window_open = bool(current_window.get("allow_trim", False) or current_window.get("allow_exit", False))
        can_buy_symbol = lifecycle in {"pilot", "build", "hold"} and symbol_state not in {"freeze", "reconcile_only"}
        can_sell_symbol = lifecycle in {"build", "hold", "trim", "exit"} and symbol_state not in {"freeze", "reconcile_only"}
        policy = resolve_t_execution_policy(config=config, row=row, timing_window=current_window.get("name", ""))

        freeze_reasons: list[str] = []
        advisory_reasons: list[str] = []
        if not timing_enabled:
            freeze_reasons.append("timing_layer_disabled")
        if str(safety_mode or "").upper() == "HALT":
            freeze_reasons.append("safety_halt")
        elif str(safety_mode or "").upper() == "PANIC" and lifecycle in {"pilot", "build"}:
            advisory_reasons.append("panic_new_risk")
        if symbol_state == "freeze":
            freeze_reasons.append(str(row.get("freeze_reason", "") or "symbol_frozen"))
        if symbol_state == "reconcile_only":
            freeze_reasons.append("symbol_reconcile_only")
        if message_veto:
            freeze_reasons.append(str(row.get("message_veto_reason", "") or "message_veto"))
        if low_liquidity:
            advisory_reasons.append("low_liquidity")
        if snapshot_missing:
            freeze_reasons.append("snapshot_unavailable")
        if proxy_stale:
            advisory_reasons.append("proxy_snapshot_stale")
        if proxy_spread >= 0.02:
            advisory_reasons.append("proxy_spread_too_wide")
        if require_oms_clean and not oms_clean_state:
            advisory_reasons.append("oms_not_clean")
        if require_flow_confirmation and not flow_ok:
            if buy_score >= buy_threshold or sell_score >= sell_threshold:
                advisory_reasons.append("flow_confirmation_missing")
        advisory_reasons.extend(str(reason) for reason in list(policy.get("reject_reasons", []) or []) if str(reason).strip())

        if freeze_reasons:
            timing_state = "reconcile_only" if "symbol_reconcile_only" in freeze_reasons else "timing_frozen"
        else:
            if buy_score >= buy_threshold and proxy_top_list_hit and proxy_tick_imbalance >= 0.10:
                buy_score = min(1.0, buy_score + 0.03)
            if sell_score >= sell_threshold and proxy_tick_imbalance <= -0.10:
                sell_score = min(1.0, sell_score + 0.03)
            buy_ready = can_buy_symbol and buy_window_open and buy_score >= buy_threshold and (flow_ok or not require_flow_confirmation)
            sell_ready = can_sell_symbol and sell_window_open and sell_score >= sell_threshold and (flow_ok or not require_flow_confirmation)
            if buy_ready and sell_ready:
                timing_state = "dual_ready"
            elif buy_ready:
                timing_state = "buy_ready"
            elif can_buy_symbol and buy_window_open:
                timing_state = "buy_watch"
            elif sell_ready:
                timing_state = "sell_ready"
            elif can_sell_symbol and sell_window_open:
                timing_state = "sell_watch"
            else:
                timing_state = "observe"

        rows.append(
            {
                "stock_code": str(row.get("stock_code", "") or ""),
                "timing_window": str(current_window.get("name", "") or ""),
                "timing_state": timing_state,
                "timing_enabled": bool(not freeze_reasons and timing_state not in {"observe"}),
                "buy_window_open": bool(buy_window_open and can_buy_symbol),
                "sell_window_open": bool(sell_window_open and can_sell_symbol),
                "oms_clean_state": bool(oms_clean_state),
                "policy_t_allowed": bool(policy.get("t_allowed", False)),
                "policy_allow_second_leg": bool(policy.get("allow_second_leg", True)),
                "policy_max_t_ratio": float(policy.get("max_t_ratio", 0.0) or 0.0),
                "policy_reject_reasons": ";".join(str(reason) for reason in list(policy.get("reject_reasons", []) or []) if str(reason).strip()),
                "timing_freeze_reason": ";".join(reason for reason in freeze_reasons if str(reason or "").strip()),
                "timing_advisory_reason": ";".join(reason for reason in advisory_reasons if str(reason or "").strip()),
                "timing_constraint_score": round(
                    max(
                        0.0,
                        1.0
                        - 0.18 * int("panic_new_risk" in advisory_reasons)
                        - 0.10 * int("oms_not_clean" in advisory_reasons)
                        - 0.08 * int("flow_confirmation_missing" in advisory_reasons)
                        - 0.12 * int("low_liquidity" in advisory_reasons)
                        - 0.10 * int("proxy_snapshot_stale" in advisory_reasons)
                        - 0.12 * int("proxy_spread_too_wide" in advisory_reasons),
                    ),
                    4,
                ),
                "proxy_spread_pct": round(proxy_spread, 6),
                "proxy_tick_imbalance": round(proxy_tick_imbalance, 6),
                "proxy_top_list_hit": bool(proxy_top_list_hit),
            }
        )
    return pd.DataFrame(rows)
