# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import is_dataclass, replace
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _series(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df.get(name), errors="coerce").fillna(0.0)
    return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")


def score_candidate_pool(
    candidate_df: pd.DataFrame,
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    thesis_summary: Dict[str, Any],
    rec_cfg: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = candidate_df.copy()
    if out.empty:
        return out, {"enabled": True, "applied": False, "reason": "empty_candidate_pool"}

    regime = str(market_state.get("market_regime", "") or "").lower()
    style_bias = str(market_state.get("style_bias", "") or "").lower()
    policy = str(market_state.get("new_position_policy", "allow") or "allow").lower()
    risk_budget = float(market_state.get("risk_budget_multiplier", 1.0) or 1.0)
    accepted = int(thesis_summary.get("n_accepted", 0) or 0)
    nav = float(account_ctx.get("nav", 0.0) or 0.0)
    positions_count = int(account_ctx.get("positions_count", 0) or 0)

    selection = _series(out, "selection_score")
    thesis = _series(out, "integrated_thesis_score")
    router = _series(out, "router_final_score")
    pred = _series(out, "pred_score_norm")
    tech = _series(out, "tech_final_score")
    event_fact = _series(out, "event_fact_backed")
    existing = _series(out, "is_existing_position")

    market_bonus = 0.0
    if regime in {"bull", "risk_on"}:
        market_bonus += 0.06
    elif regime in {"panic"}:
        market_bonus -= 0.10
    if style_bias in {"aggressive", "growth"}:
        market_bonus += 0.04

    thesis_relax = 0.08 if accepted <= max(int(rec_cfg.get("llm_candidate_weak_accept_threshold", 1) or 1), 1) else 0.0
    out["outer_intelligence_score"] = (
        selection * 0.30
        + thesis * 0.26
        + router * 0.16
        + pred * 0.12
        + tech * 0.06
        + event_fact * 0.06
        + existing * 0.04
        + thesis_relax
        + market_bonus
    ).clip(lower=0.0)

    out["outer_intelligence_weight_multiplier"] = 1.0
    out.loc[_series(out, "router_allow_entry").fillna(1.0).le(0), "outer_intelligence_weight_multiplier"] *= 0.82
    out.loc[_series(out, "tech_allow_entry").fillna(1.0).le(0), "outer_intelligence_weight_multiplier"] *= 0.86
    if policy in {"reduce_only", "no_new_positions"}:
        out.loc[_series(out, "is_existing_position").le(0), "outer_intelligence_weight_multiplier"] *= 0.35
    elif policy == "tight":
        out.loc[_series(out, "is_existing_position").le(0), "outer_intelligence_weight_multiplier"] *= 0.72

    if nav > 0 and nav < 50000 and positions_count <= 0:
        out["outer_intelligence_weight_multiplier"] *= 0.85

    out["outer_intelligence_weight_multiplier"] = out["outer_intelligence_weight_multiplier"].clip(lower=0.20, upper=1.45)
    out["outer_intelligence_priority"] = out["outer_intelligence_score"] * out["outer_intelligence_weight_multiplier"] * max(0.35, min(risk_budget, 1.15))
    out["outer_intelligence_action"] = "deploy"
    out.loc[out["outer_intelligence_priority"] < 0.16, "outer_intelligence_action"] = "observe"
    out.loc[
        out["outer_intelligence_priority"] < 0.10,
        "outer_intelligence_action",
    ] = "shadow"

    if "portfolio_weight" in out.columns:
        out["portfolio_weight"] = _series(out, "portfolio_weight") * out["outer_intelligence_weight_multiplier"]

    out = out.sort_values(
        ["outer_intelligence_priority", "selection_score", "integrated_thesis_score", "router_final_score"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    return out, {
        "enabled": True,
        "applied": True,
        "market_regime": regime,
        "style_bias": style_bias,
        "risk_budget_multiplier": risk_budget,
        "accepted_thesis_count": accepted,
        "positions_count": positions_count,
        "top_priority_mean": float(out["outer_intelligence_priority"].head(10).mean()) if not out.empty else 0.0,
        "deploy_count": int((out["outer_intelligence_action"] == "deploy").sum()),
        "observe_count": int((out["outer_intelligence_action"] == "observe").sum()),
        "shadow_count": int((out["outer_intelligence_action"] == "shadow").sum()),
    }


def arbitrate_execution(
    safety: Dict[str, Any],
    market_state: Dict[str, Any],
    llm_review: Dict[str, Any],
    account_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    system_mode = str(safety.get("system_mode", "") or "").upper()
    market_regime = str(safety.get("market_safety_regime", "") or "").lower()
    broker_ok = bool(safety.get("broker_reachable", True))
    positions_count = int(account_snapshot.get("positions_count", 0) or 0)
    fail_ratio = float(safety.get("execution_fail_ratio", 0.0) or 0.0)
    requested_reduce_only = bool((llm_review.get("review") or {}).get("reduce_only", False))

    hard_block = False
    reasons = []
    if system_mode == "HALT":
        hard_block = True
        reasons.append("system_halt")
    if market_regime == "panic":
        hard_block = True
        reasons.append("market_panic")
    if not broker_ok and positions_count > 0:
        hard_block = True
        reasons.append("broker_unreachable_with_positions")

    if hard_block:
        return {
            "verdict": "block",
            "reduce_only": True,
            "turnover_multiplier": 0.0,
            "size_multiplier": 0.0,
            "summary": ",".join(reasons),
        }

    turnover = min(
        float(safety.get("effective_turnover_multiplier", 1.0) or 1.0),
        float(market_state.get("turnover_multiplier", 1.0) or 1.0),
        float((llm_review.get("review") or {}).get("turnover_multiplier", 1.0) or 1.0),
    )
    verdict = "proceed"
    reduce_only = False
    if requested_reduce_only or fail_ratio >= 0.85:
        verdict = "reduce_only"
        reduce_only = True
        turnover = min(turnover, 0.45)
    elif fail_ratio >= 0.35 or market_regime == "caution":
        verdict = "proceed_degraded"
        turnover = min(turnover, 0.78)

    return {
        "verdict": verdict,
        "reduce_only": reduce_only,
        "turnover_multiplier": max(turnover, 0.08),
        "size_multiplier": 0.55 if reduce_only else (0.82 if verdict == "proceed_degraded" else 1.0),
        "summary": f"verdict={verdict} market={market_regime or 'normal'} fail_ratio={fail_ratio:.2f}",
    }


def _target_diversification_slots(nav: float) -> int:
    if nav <= 0:
        return 6
    if nav < 50000:
        return 5
    if nav < 150000:
        return 8
    if nav < 500000:
        return 12
    if nav < 1500000:
        return 16
    return 22


def _account_bucket(nav: float) -> str:
    if nav <= 0:
        return "unknown"
    if nav < 50000:
        return "micro"
    if nav < 150000:
        return "small"
    if nav < 500000:
        return "mid"
    if nav < 1500000:
        return "large"
    return "institutional"


def arbitrate_intraday_intents(
    intents: Iterable[Any],
    *,
    ctx: Dict[str, Any],
    policy: Any,
) -> Tuple[List[Any], Dict[str, Any]]:
    items = list(intents or [])
    if not items:
        return [], {"enabled": True, "applied": False, "reason": "empty_intents"}

    clock = dict(ctx.get("clock_account_snapshot", {}) or {})
    overlay = dict(dict(ctx.get("control_summary", {}) or {}).get("overlay_recommendation", {}) or {})
    trade_discipline = dict(dict(ctx.get("portfolio_summary", {}) or {}).get("trade_discipline", {}) or {})
    symbol_frame = ctx.get("symbol_frame")
    symbol_rows: Dict[str, Dict[str, Any]] = {}
    if isinstance(symbol_frame, pd.DataFrame) and not symbol_frame.empty:
        for _, series in symbol_frame.iterrows():
            row = dict(series.to_dict())
            symbol = str(row.get("stock_code") or row.get("symbol") or row.get("ts_code") or "").strip().upper()
            if symbol:
                symbol_rows[symbol] = row

    nav = float(clock.get("nav", 0.0) or 0.0)
    cash = float(clock.get("cash", clock.get("available_cash", 0.0)) or 0.0)
    positions_count = int(clock.get("positions_count", 0) or 0)
    cash_ratio = cash / max(nav, 1e-9) if nav > 0 else 0.0
    account_bucket = _account_bucket(nav)
    concentration_risk = str(clock.get("concentration_risk", "") or "").lower()
    top1_weight = float(clock.get("concentration_top1_weight", 0.0) or 0.0)
    block_new_entries = bool(overlay.get("block_new_entries", False))
    block_new_t = bool(overlay.get("block_new_t", False))
    panic_degrade_only = bool(overlay.get("panic_degrade_only", False))
    force_reconcile_only = bool(overlay.get("force_reconcile_only", False))
    discipline_posture = str(trade_discipline.get("posture", "balanced") or "balanced").strip().lower()
    discipline_sell_pressure = float(trade_discipline.get("sell_pressure", 0.0) or 0.0)
    discipline_add_multiplier = float(trade_discipline.get("add_multiplier", 1.0) or 1.0)
    discipline_prefer_families = {str(item).strip().lower() for item in list(trade_discipline.get("promote_families", []) or []) if str(item).strip()}
    discipline_demote_families = {str(item).strip().lower() for item in list(trade_discipline.get("demote_families", []) or []) if str(item).strip()}
    target_slots = _target_diversification_slots(nav)
    diversification_cap = 1.0 / max(target_slots, 1)
    intelligence_rows: List[Any] = []
    suppressed = 0
    buy_kept = 0
    sell_kept = 0

    for intent in items:
        symbol = str(getattr(intent, "symbol", "") or "").strip().upper()
        side = str(getattr(intent, "side", "") or "").strip().upper()
        intent_class = str(getattr(intent, "intent_class", "") or "").strip()
        reason_code = str(getattr(intent, "reason_code", "") or "").strip()
        symbol_row = dict(symbol_rows.get(symbol, {}) or {})
        actual_weight = float(symbol_row.get("actual_weight", 0.0) or 0.0)
        timing_state = str(symbol_row.get("timing_state", "") or "").strip().lower()
        timing_freeze_reason = str(symbol_row.get("timing_freeze_reason", "") or "").strip().lower()
        timing_advisory_reason = str(symbol_row.get("timing_advisory_reason", "") or "").strip().lower()
        timing_constraint_score = float(symbol_row.get("timing_constraint_score", 1.0) or 1.0)
        feature_tier = str(getattr(intent, "feature_quality_tier", "") or "").strip().lower()
        debug_payload = dict(getattr(intent, "debug_payload", {}) or {})
        portfolio_role = str(debug_payload.get("portfolio_service_role", "") or "").strip().lower()
        portfolio_priority = float(debug_payload.get("portfolio_service_priority", 0.0) or 0.0)
        alpha_family = str(debug_payload.get("alpha_family", "") or "").strip().lower()
        degraded = "stale" in feature_tier or feature_tier in {"degraded", "proxy_degraded"}
        multiplier = 1.0
        suppress_reason = ""

        severe_freeze = any(
            token in timing_freeze_reason
            for token in ("safety_halt", "symbol_reconcile_only", "snapshot_unavailable", "message_veto", "symbol_frozen")
        )

        if timing_state == "reconcile_only" and side == "BUY":
            suppress_reason = "timing_reconcile_only"
        elif force_reconcile_only and side == "BUY":
            suppress_reason = "force_reconcile_only"
        elif block_new_entries and side == "BUY":
            suppress_reason = "block_new_entries"
        elif block_new_t and side == "BUY" and intent_class == "t_overlay":
            suppress_reason = "block_new_t"
        elif timing_state == "timing_frozen" and side == "BUY" and severe_freeze:
            suppress_reason = "severe_timing_freeze"
        else:
            if side == "BUY":
                if panic_degrade_only:
                    multiplier *= 0.58
                if timing_state == "timing_frozen":
                    multiplier *= 0.42
                multiplier *= _clip(timing_constraint_score, 0.35, 1.0)
                if "panic_new_risk" in timing_advisory_reason:
                    multiplier *= 0.72
                if "oms_not_clean" in timing_advisory_reason:
                    multiplier *= 0.86
                if "flow_confirmation_missing" in timing_advisory_reason:
                    multiplier *= 0.90
                if "low_liquidity" in timing_advisory_reason:
                    multiplier *= 0.72
                if "proxy_snapshot_stale" in timing_advisory_reason or "proxy_spread_too_wide" in timing_advisory_reason:
                    multiplier *= 0.74
                if degraded:
                    multiplier *= 0.72 if bool(getattr(policy, "allow_add_on_snapshot_degraded", True)) else 0.48
                if concentration_risk == "high":
                    multiplier *= 0.38
                elif concentration_risk == "elevated":
                    multiplier *= 0.62
                if top1_weight >= 0.25 and actual_weight >= 0.12:
                    multiplier *= 0.42
                if cash_ratio < 0.05:
                    multiplier *= 0.35
                elif cash_ratio < 0.10:
                    multiplier *= 0.55
                elif cash_ratio < 0.18:
                    multiplier *= 0.75
                if positions_count < target_slots and actual_weight <= diversification_cap * 0.6:
                    multiplier *= 1.06
                if account_bucket == "micro":
                    multiplier *= 0.88
                elif account_bucket == "small":
                    multiplier *= 0.94
                if portfolio_role == "rebuild_core":
                    multiplier *= 1.08 if actual_weight <= diversification_cap * 0.85 else 0.94
                elif portfolio_role == "expand_diversified_winner":
                    multiplier *= 1.12 if actual_weight <= diversification_cap * 0.6 else 0.82
                if discipline_posture == "reduce_only":
                    multiplier *= 0.0
                elif discipline_posture == "defensive":
                    multiplier *= 0.72
                multiplier *= _clip(discipline_add_multiplier, 0.0, 1.25)
                if alpha_family and alpha_family in discipline_prefer_families:
                    multiplier *= 1.06
                if alpha_family and alpha_family in discipline_demote_families:
                    multiplier *= 0.62
            else:
                if portfolio_role == "reduce_risk":
                    multiplier *= 1.12
                elif portfolio_role == "harvest_and_rotate":
                    multiplier *= 1.06 if actual_weight >= diversification_cap * 0.75 else 0.94
                if concentration_risk == "high" and reason_code in {"risk_concentration_top1", "risk_concentration_hhi"}:
                    multiplier *= 1.18
                if panic_degrade_only and reason_code in {"sl_soft", "tp_soft"}:
                    multiplier *= 0.92
                if timing_state == "timing_frozen" and severe_freeze:
                    multiplier *= 1.08
                multiplier *= 1.0 + min(max(discipline_sell_pressure, 0.0), 1.0) * 0.18
                if alpha_family and alpha_family in discipline_demote_families:
                    multiplier *= 1.10

        if suppress_reason:
            suppressed += 1
            continue

        current_shares = int(getattr(intent, "delta_shares", 0) or 0)
        current_cap = float(getattr(intent, "delta_notional_cap", 0.0) or 0.0)
        if side == "BUY" and nav > 0:
            buy_ratio_cap = min(
                float(getattr(policy, "max_symbol_add_ratio", 0.06) or 0.06),
                diversification_cap * (1.25 if positions_count < max(target_slots // 2, 1) else 1.0),
                0.12 if nav < 50000 else 0.09 if nav < 150000 else 0.07 if nav < 500000 else 0.055,
            )
            current_cap = min(current_cap if current_cap > 0 else nav * buy_ratio_cap, nav * buy_ratio_cap)
        scaled_shares = int(max(current_shares * multiplier, 0) // 100 * 100)
        if current_shares > 0 and scaled_shares <= 0:
            scaled_shares = 100 if side == "SELL" else 0
        if scaled_shares <= 0:
            suppressed += 1
            continue

        debug_payload["outer_intelligence_intraday"] = {
            "multiplier": round(multiplier, 4),
            "target_slots": int(target_slots),
            "account_bucket": account_bucket,
            "cash_ratio": round(cash_ratio, 4),
            "concentration_risk": concentration_risk,
            "panic_degrade_only": panic_degrade_only,
            "block_new_entries": block_new_entries,
            "actual_weight": round(actual_weight, 4),
            "timing_state": timing_state,
            "timing_constraint_score": round(timing_constraint_score, 4),
            "portfolio_service_role": portfolio_role,
            "portfolio_service_priority": round(portfolio_priority, 4),
            "discipline_posture": discipline_posture,
            "discipline_sell_pressure": round(discipline_sell_pressure, 4),
            "alpha_family": alpha_family,
        }
        updated = replace(
            intent,
            delta_shares=int(scaled_shares),
            delta_notional_cap=float(current_cap) if current_cap > 0 else float(getattr(intent, "delta_notional_cap", 0.0) or 0.0),
            debug_payload=debug_payload,
        ) if is_dataclass(intent) else intent
        intelligence_rows.append(updated)
        if side == "BUY":
            buy_kept += 1
        else:
            sell_kept += 1

    return intelligence_rows, {
        "enabled": True,
        "applied": True,
        "input_intents": int(len(items)),
        "kept_intents": int(len(intelligence_rows)),
        "suppressed_intents": int(suppressed),
        "buy_kept": int(buy_kept),
        "sell_kept": int(sell_kept),
        "positions_count": int(positions_count),
        "target_diversification_slots": int(target_slots),
        "account_bucket": account_bucket,
        "cash_ratio": round(cash_ratio, 4),
        "concentration_risk": concentration_risk,
        "panic_degrade_only": panic_degrade_only,
        "block_new_entries": block_new_entries,
        "force_reconcile_only": force_reconcile_only,
        "discipline_posture": discipline_posture,
        "discipline_sell_pressure": round(discipline_sell_pressure, 4),
    }
