from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

import pandas as pd

from .intent_schema import INTENT_CLASS_PRIORITY, IntradayActionIntent
from .policy import TacticalPolicy


def _sym(row: Dict[str, Any]) -> str:
    return str(row.get("stock_code") or row.get("symbol") or row.get("ts_code") or "").strip().upper()


def _f(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = float(row.get(key, default) or default)
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return float(default)
        return v
    except Exception:
        return float(default)


def _lifecycle(row: Dict[str, Any]) -> str:
    return str(row.get("source_lifecycle_state", "") or row.get("lifecycle_state", "") or "").strip().lower()


def _window(row: Dict[str, Any]) -> str:
    return str(row.get("timing_window_name", "") or row.get("timing_window", "") or "").strip()


def _feat(row: Dict[str, Any]) -> str:
    return str(row.get("feature_quality_tier", "") or "").strip()


def _allows_buy_window(window: str) -> bool:
    w = str(window or "").lower()
    if "open_noise" in w or "noise" in w:
        return False
    if "post_1450" in w or "close_only" in w:
        return False
    return True


def _lifecycle_allows_add(lifecycle: str) -> bool:
    return lifecycle in {"build", "hold", "pilot"}


def _max_shares_for_ratio(nav: float, price: float, ratio: float, lot: int) -> int:
    if nav <= 0 or price <= 0 or ratio <= 0:
        return 0
    cap_val = nav * ratio
    raw = int(cap_val / price)
    return max((raw // max(lot, 1)) * max(lot, 1), 0)


def run_triggers(
    *,
    ctx: Dict[str, Any],
    policy: TacticalPolicy,
    tactical_phase: str,
    now: datetime | None = None,
) -> List[IntradayActionIntent]:
    now = now or datetime.now()
    created = now.strftime("%Y-%m-%d %H:%M:%S")
    trade_date = str(ctx.get("trade_date", "") or "")
    frame: pd.DataFrame = ctx.get("symbol_frame") if isinstance(ctx.get("symbol_frame"), pd.DataFrame) else pd.DataFrame()
    if frame is None or frame.empty:
        return []

    clock = dict(ctx.get("clock_account_snapshot", {}) or {})
    conc = str(clock.get("concentration_risk", "") or "").lower()
    ctrl = dict(ctx.get("control_summary", {}) or {})
    overlay = dict(ctrl.get("overlay_recommendation", {}) or {})
    block_new = bool(overlay.get("block_new_entries", False))
    block_new_t = bool(overlay.get("block_new_t", False))

    intents: List[IntradayActionIntent] = []
    nav = float(clock.get("nav", 0) or 0)
    lot = 100

    for _, series in frame.iterrows():
        row = dict(series.to_dict())
        symbol = _sym(row)
        if not symbol:
            continue
        lifecycle = _lifecycle(row)
        window = _window(row)
        tier = _feat(row)
        degraded = "stale" in tier.lower() or tier.lower() in {"degraded", "proxy_degraded"}
        price = _f(row, "proxy_last_price") or _f(row, "last_price_ref")
        if price <= 0:
            price = _f(row, "last_price", 0.01)
        actual_w = _f(row, "actual_weight")
        unreal = _f(row, "unrealized_pnl_pct") / 100.0 if _f(row, "unrealized_pnl_pct") else 0.0
        if unreal == 0.0 and _f(row, "cost_basis_proxy", 0) > 0:
            unreal = (_f(row, "proxy_last_price") - _f(row, "cost_basis_proxy")) / max(_f(row, "cost_basis_proxy"), 1e-9)

        raw_sh = _f(row, "actual_shares_proxy", 0)
        shares_est = int(raw_sh) if raw_sh > 0 else int(nav * actual_w / max(price, 1e-9) // 100 * 100)

        msg_veto = bool(row.get("message_veto_flag", False))
        if msg_veto and actual_w > 0:
            ds = max((shares_est // lot) * lot, lot) if shares_est > 0 else 0
            if ds > 0:
                intents.append(
                    _intent(
                        trade_date,
                        symbol,
                        "SELL",
                        "event_veto_exit",
                        "major_event_veto",
                        "event_veto",
                        ds,
                        nav * policy.max_symbol_reduce_ratio,
                        False,
                        True,
                        window,
                        str(row.get("timing_state", "") or ""),
                        str(row.get("t_overlay_state", "") or ""),
                        tier,
                        lifecycle,
                        str(row.get("mechanism_primary", "") or ""),
                        str(row.get("primary_event_type", "") or ""),
                        created,
                        tactical_phase,
                        {"row": "message_veto"},
                    )
                )

        if policy.enable_stop_loss and unreal <= -policy.sl_hard_pct and actual_w > 0:
            ds = max((int(shares_est * 0.95) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "stop_loss",
                    "stop_loss_pct",
                    "sl_hard",
                    ds,
                    nav * policy.max_symbol_reduce_ratio,
                    unreal <= -policy.sl_hard_pct * 1.1,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {"unreal": unreal},
                )
            )
        elif policy.enable_stop_loss and unreal <= -policy.sl_soft_pct and actual_w > 0 and lifecycle in {"pilot", "build"}:
            ds = max((int(shares_est * 0.35) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "stop_loss",
                    "stop_loss_pct",
                    "sl_soft",
                    ds,
                    nav * policy.max_symbol_reduce_ratio * 0.5,
                    False,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {"unreal": unreal},
                )
            )

        if policy.enable_take_profit and unreal >= policy.tp_hard_pct and actual_w > 0:
            ds = max((int(shares_est * 0.5) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "take_profit",
                    "take_profit_pct",
                    "tp_hard",
                    ds,
                    nav * policy.max_symbol_reduce_ratio,
                    False,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {"unreal": unreal},
                )
            )
        elif policy.enable_take_profit and unreal >= policy.tp_soft_pct and actual_w > 0:
            ds = max((int(shares_est * 0.2) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "take_profit",
                    "take_profit_intraday_spike",
                    "tp_soft",
                    ds,
                    nav * policy.max_symbol_reduce_ratio * 0.4,
                    False,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {"unreal": unreal},
                )
            )

        if policy.enable_time_stop and actual_w > 0 and policy.time_stop_minutes > 0:
            if unreal < policy.tp_soft_pct * 0.2 and unreal > -policy.sl_soft_pct * 0.5:
                ds = max((int(shares_est * 0.12) // lot) * lot, 0)
                if ds > 0:
                    intents.append(
                        _intent(
                            trade_date,
                            symbol,
                            "SELL",
                            "time_stop",
                            "time_stop_no_followthrough",
                            "time_soft",
                            ds,
                            nav * 0.05,
                            False,
                            True,
                            window,
                            str(row.get("timing_state", "") or ""),
                            str(row.get("t_overlay_state", "") or ""),
                            tier,
                            lifecycle,
                            str(row.get("mechanism_primary", "") or ""),
                            str(row.get("primary_event_type", "") or ""),
                            created,
                            tactical_phase,
                            {},
                        )
                    )

        if conc in {"high", "elevated"} and actual_w > float(clock.get("concentration_top1_weight", 1) or 1) * 0.5 and actual_w > 0:
            ds = max((int(shares_est * 0.15) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "concentration_reduce",
                    "risk_concentration_top1" if conc == "high" else "risk_concentration_hhi",
                    "conc",
                    ds,
                    nav * 0.08,
                    False,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {"conc": conc},
                )
            )

        if degraded and actual_w > 0 and policy.allow_reduce_on_snapshot_degraded:
            ds = max((int(shares_est * 0.1) // lot) * lot, 0)
            if ds > 0:
                intents.append(
                    _intent(
                        trade_date,
                        symbol,
                        "SELL",
                        "liquidity_reduce",
                        "proxy_stale",
                        "liq_degraded",
                        ds,
                        nav * 0.04,
                        False,
                        True,
                        window,
                        str(row.get("timing_state", "") or ""),
                        str(row.get("t_overlay_state", "") or ""),
                        tier,
                        lifecycle,
                        str(row.get("mechanism_primary", "") or ""),
                        str(row.get("primary_event_type", "") or ""),
                        created,
                        tactical_phase,
                        {"tier": tier},
                    )
                )

        if str(row.get("signal_decay_flag", "")).lower() in {"1", "true", "yes"} and actual_w > 0:
            ds = max((int(shares_est * 0.18) // lot) * lot, lot)
            intents.append(
                _intent(
                    trade_date,
                    symbol,
                    "SELL",
                    "signal_decay_reduce",
                    "signal_decay",
                    "sig_decay",
                    ds,
                    nav * 0.06,
                    False,
                    True,
                    window,
                    str(row.get("timing_state", "") or ""),
                    str(row.get("t_overlay_state", "") or ""),
                    tier,
                    lifecycle,
                    str(row.get("mechanism_primary", "") or ""),
                    str(row.get("primary_event_type", "") or ""),
                    created,
                    tactical_phase,
                    {},
                )
            )

        if policy.enable_t_overlay:
            ts = str(row.get("t_overlay_state", "") or "")
            if ts in {"t_armed", "t_sell_leg_done_wait_buyback", "t_buy_leg_done_wait_sellback"} and bool(row.get("t_triggered", False)):
                side = "SELL" if "sell" in ts or ts == "t_armed" else "BUY"
                ds = max(_max_shares_for_ratio(nav, price, float(row.get("t_allowed_ratio", 0.08) or 0.08), lot), lot)
                intents.append(
                    _intent(
                        trade_date,
                        symbol,
                        side,
                        "t_overlay",
                        "t_first_leg" if ts == "t_armed" else "t_second_leg",
                        "t_overlay",
                        ds,
                        nav * 0.05,
                        False,
                        side == "SELL",
                        window,
                        str(row.get("timing_state", "") or ""),
                        ts,
                        tier,
                        lifecycle,
                        str(row.get("mechanism_primary", "") or ""),
                        str(row.get("primary_event_type", "") or ""),
                        created,
                        tactical_phase,
                        {
                            "t_state": ts,
                            "block_new_t": block_new_t,
                            "degraded": degraded,
                            "portfolio_service_role": "reduce_risk" if side == "SELL" else "rebuild_core",
                            "portfolio_service_priority": 0.92 if side == "SELL" else 0.68,
                            "alpha_family": str(row.get("activation_alpha_family", "") or row.get("alpha_family", "") or ""),
                        },
                    )
                )

        if (
            policy.enable_tactical_add
            and _lifecycle_allows_add(lifecycle)
            and _allows_buy_window(window)
            and float(row.get("buy_timing_score", 0) or 0) >= 0.62
            and float(row.get("buy_timing_score", 0) or 0) > float(row.get("sell_timing_score", 0) or 0) + 0.04
        ):
            add_cap = _max_shares_for_ratio(nav, price, min(policy.max_symbol_add_ratio, float(row.get("executable_headroom_ratio", 0.03) or 0.03)), lot)
            if add_cap > 0:
                intents.append(
                    _intent(
                        trade_date,
                        symbol,
                        "BUY",
                        "tactical_add",
                        "pullback_add",
                        "tactical_add",
                        add_cap,
                        nav * policy.max_symbol_add_ratio,
                        False,
                        False,
                        window,
                        str(row.get("timing_state", "") or ""),
                        str(row.get("t_overlay_state", "") or ""),
                        tier,
                        lifecycle,
                        str(row.get("mechanism_primary", "") or ""),
                        str(row.get("primary_event_type", "") or ""),
                        created,
                        tactical_phase,
                        {
                            "buy_score": float(row.get("buy_timing_score", 0) or 0),
                            "block_new_entries": block_new,
                            "degraded": degraded,
                            "concentration_risk": conc,
                            "portfolio_service_role": "expand_diversified_winner",
                            "portfolio_service_priority": 0.54 if actual_w <= 0.04 else 0.38,
                            "alpha_family": str(row.get("activation_alpha_family", "") or row.get("alpha_family", "") or ""),
                        },
                    )
                )

    return intents


def _intent(
    trade_date: str,
    symbol: str,
    side: str,
    intent_class: str,
    reason_code: str,
    rule_id: str,
    delta_shares: int,
    delta_notional_cap: float,
    full_exit: bool,
    reduce_only: bool,
    window: str,
    timing_state: str,
    t_overlay_state: str,
    tier: str,
    lifecycle: str,
    mech: str,
    ev: str,
    created: str,
    phase: str,
    debug: Dict[str, Any],
) -> IntradayActionIntent:
    pri = INTENT_CLASS_PRIORITY.get(intent_class, 99)
    payload = dict(debug or {})
    if "portfolio_service_role" not in payload:
        if str(side).upper() == "SELL":
            if intent_class in {"stop_loss", "event_veto_exit", "concentration_reduce", "liquidity_reduce", "signal_decay_reduce"}:
                payload["portfolio_service_role"] = "reduce_risk"
                payload["portfolio_service_priority"] = 0.95
            elif intent_class in {"take_profit", "time_stop"}:
                payload["portfolio_service_role"] = "harvest_and_rotate"
                payload["portfolio_service_priority"] = 0.72
        elif intent_class == "tactical_add":
            payload["portfolio_service_role"] = "expand_diversified_winner"
            payload["portfolio_service_priority"] = 0.48
        elif intent_class == "t_overlay":
            payload["portfolio_service_role"] = "rebuild_core"
            payload["portfolio_service_priority"] = 0.62
    return IntradayActionIntent(
        intent_id=f"iai_{uuid4().hex[:12]}",
        trade_date=trade_date,
        symbol=symbol,
        side=str(side).upper(),
        intent_class=intent_class,
        reason_code=reason_code,
        rule_id=rule_id,
        priority=int(pri),
        delta_shares=int(max(delta_shares, 0)),
        delta_notional_cap=float(delta_notional_cap),
        full_exit_flag=bool(full_exit),
        reduce_only=bool(reduce_only),
        allow_conflict_with_release=str(intent_class) in {"manual_override", "risk_exit", "event_veto_exit", "stop_loss"},
        source_window=str(window or ""),
        timing_state=str(timing_state or ""),
        t_overlay_state=str(t_overlay_state or ""),
        feature_quality_tier=str(tier or ""),
        lifecycle_state=str(lifecycle or ""),
        mechanism_primary=str(mech or ""),
        primary_event_type=str(ev or ""),
        cooldown_key=f"{symbol}|{intent_class}|{reason_code}",
        cooldown_until="",
        max_rounds_today=4,
        valid_from=created,
        valid_until="",
        created_at=created,
        created_by_module=f"intraday_tactics.trigger_engine@{phase}",
        debug_payload=payload,
    )
