# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import AccountState, FillRecord, OrderIntent, Position, TargetPosition
from .utils import dump_json, ensure_dir, safe_float, safe_int


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _positions_to_map(positions: Iterable[Position]) -> Dict[str, Position]:
    return {str(pos.symbol): pos for pos in positions}


def load_control_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(config.get("portfolio_control", {}) or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "drift_threshold": max(0.0, safe_float(raw.get("drift_threshold", 0.005), 0.005)),
        "max_daily_turnover_ratio": max(0.0, safe_float(raw.get("max_daily_turnover_ratio", 0.25), 0.25)),
        "dynamic_account_scaling_enabled": bool(raw.get("dynamic_account_scaling_enabled", True)),
        "dynamic_account_max_turnover_ratio": max(0.05, safe_float(raw.get("dynamic_account_max_turnover_ratio", 0.9), 0.9)),
        "dynamic_account_max_single_buy_nav_ratio_cap": max(0.05, safe_float(raw.get("dynamic_account_max_single_buy_nav_ratio_cap", 0.45), 0.45)),
        "dynamic_account_max_single_buy_cash_ratio_cap": max(0.05, safe_float(raw.get("dynamic_account_max_single_buy_cash_ratio_cap", 0.5), 0.5)),
        "enable_execution_feedback": bool(raw.get("enable_execution_feedback", True)),
        "enable_dev_log_snapshot": bool(raw.get("enable_dev_log_snapshot", True)),
        "dev_log_top_holdings": max(3, safe_int(raw.get("dev_log_top_holdings", 8), 8)),
        "allow_odd_lot_exit": bool(raw.get("allow_odd_lot_exit", True)),
        "reduce_only": bool(raw.get("reduce_only", False)),
        "preferred_t_mechanism": str(raw.get("preferred_t_mechanism", "") or "").strip(),
        "preferred_t_mechanism_enforced": bool(raw.get("preferred_t_mechanism_enforced", False)),
        "preferred_t_mechanism_buy_only": bool(raw.get("preferred_t_mechanism_buy_only", True)),
        "bootstrap_diversification_enabled": bool(raw.get("bootstrap_diversification_enabled", True)),
        "bootstrap_max_current_exposure_ratio": max(0.0, safe_float(raw.get("bootstrap_max_current_exposure_ratio", 0.05), 0.05)),
        "bootstrap_min_names": max(1, safe_int(raw.get("bootstrap_min_names", 5), 5)),
        "bootstrap_slot_budget_ratio": max(0.1, safe_float(raw.get("bootstrap_slot_budget_ratio", 0.9), 0.9)),
        "small_account_slicing_enabled": bool(raw.get("small_account_slicing_enabled", True)),
        "small_account_nav_threshold": max(0.0, safe_float(raw.get("small_account_nav_threshold", 50000.0), 50000.0)),
        "small_account_max_single_buy_nav_ratio": max(0.05, safe_float(raw.get("small_account_max_single_buy_nav_ratio", 0.22), 0.22)),
        "small_account_max_single_buy_cash_ratio": max(0.05, safe_float(raw.get("small_account_max_single_buy_cash_ratio", 0.28), 0.28)),
        "llm_blocked_symbols": [str(item).strip().upper() for item in list(raw.get("llm_blocked_symbols", []) or []) if str(item).strip()],
        "llm_favored_symbols": [str(item).strip().upper() for item in list(raw.get("llm_favored_symbols", []) or []) if str(item).strip()],
        "llm_favored_score_boost": max(0.0, safe_float(raw.get("llm_favored_score_boost", 75.0), 75.0)),
        "codex_dev_log_path": str(raw.get("codex_dev_log_path", "") or "").strip(),
    }


def _min_executable_notional(symbol: str, price_map: Dict[str, float], lot_size: int, min_trade_value: float) -> float:
    price = safe_float(price_map.get(symbol, 0.0), 0.0)
    if price <= 0:
        return 0.0
    return max(float(min_trade_value), float(lot_size) * price)


def _derive_dynamic_account_control(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    lot_size: int,
    min_trade_value: float,
    control_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    effective_cfg = dict(control_cfg or {})
    nav = max(float(account_state.nav()), 0.0)
    cash = max(float(account_state.cash), 0.0)
    current_exposure_ratio = 0.0 if nav <= 0 else max((nav - cash) / nav, 0.0)
    summary = {
        "dynamic_enabled": bool(effective_cfg.get("dynamic_account_scaling_enabled", True)),
        "account_nav": nav,
        "account_cash": cash,
        "current_exposure_ratio": current_exposure_ratio,
        "affordable_names": 0,
        "min_required_notional_top_bucket": 0.0,
        "effective_max_daily_turnover_ratio": float(effective_cfg.get("max_daily_turnover_ratio", 0.0) or 0.0),
        "effective_bootstrap_min_names": int(effective_cfg.get("bootstrap_min_names", 1) or 1),
        "effective_max_single_buy_nav_ratio": float(effective_cfg.get("small_account_max_single_buy_nav_ratio", 0.0) or 0.0),
        "effective_max_single_buy_cash_ratio": float(effective_cfg.get("small_account_max_single_buy_cash_ratio", 0.0) or 0.0),
    }
    if nav <= 0 or not bool(effective_cfg.get("dynamic_account_scaling_enabled", True)):
        return effective_cfg, summary
    notionals: List[float] = []
    for item in list(target_positions or []):
        required = _min_executable_notional(str(item.symbol), price_map=price_map, lot_size=lot_size, min_trade_value=min_trade_value)
        if required > 0:
            notionals.append(required)
    notionals = sorted(notionals)
    if not notionals:
        return effective_cfg, summary
    spend = 0.0
    affordable_names = 0
    for value in notionals:
        if spend + value > cash + 1e-9:
            break
        spend += value
        affordable_names += 1
    affordable_names = max(affordable_names, 1 if cash >= notionals[0] else 0)
    summary["affordable_names"] = int(affordable_names)
    summary["min_required_notional_top_bucket"] = float(notionals[min(max(affordable_names - 1, 0), len(notionals) - 1)])
    if affordable_names <= 0:
        return effective_cfg, summary
    if current_exposure_ratio <= float(effective_cfg.get("bootstrap_max_current_exposure_ratio", 0.05) or 0.05):
        required_turnover_ratio = min(
            float(effective_cfg.get("dynamic_account_max_turnover_ratio", 0.9) or 0.9),
            max(
                float(effective_cfg.get("max_daily_turnover_ratio", 0.0) or 0.0),
                (spend / max(nav, 1e-9)) * 1.08,
            ),
        )
        effective_cfg["max_daily_turnover_ratio"] = required_turnover_ratio
    effective_cfg["bootstrap_min_names"] = max(1, min(int(effective_cfg.get("bootstrap_min_names", 1) or 1), affordable_names))
    per_name_value = min(cash, nav) / max(affordable_names, 1)
    effective_cfg["small_account_max_single_buy_nav_ratio"] = min(
        float(effective_cfg.get("dynamic_account_max_single_buy_nav_ratio_cap", 0.45) or 0.45),
        max(float(effective_cfg.get("small_account_max_single_buy_nav_ratio", 0.22) or 0.22), (per_name_value / max(nav, 1e-9)) * 1.15),
    )
    effective_cfg["small_account_max_single_buy_cash_ratio"] = min(
        float(effective_cfg.get("dynamic_account_max_single_buy_cash_ratio_cap", 0.5) or 0.5),
        max(float(effective_cfg.get("small_account_max_single_buy_cash_ratio", 0.28) or 0.28), (per_name_value / max(cash, 1e-9)) * 1.15),
    )
    summary["effective_max_daily_turnover_ratio"] = float(effective_cfg.get("max_daily_turnover_ratio", 0.0) or 0.0)
    summary["effective_bootstrap_min_names"] = int(effective_cfg.get("bootstrap_min_names", 1) or 1)
    summary["effective_max_single_buy_nav_ratio"] = float(effective_cfg.get("small_account_max_single_buy_nav_ratio", 0.0) or 0.0)
    summary["effective_max_single_buy_cash_ratio"] = float(effective_cfg.get("small_account_max_single_buy_cash_ratio", 0.0) or 0.0)
    return effective_cfg, summary


def _target_maps(target_positions: List[TargetPosition]) -> Tuple[Dict[str, float], Dict[str, float]]:
    target_weight_map: Dict[str, float] = {}
    target_score_map: Dict[str, float] = {}
    for item in target_positions:
        target_weight_map[str(item.symbol)] = float(item.target_weight)
        target_score_map[str(item.symbol)] = safe_float(item.score, 0.0)
    return target_weight_map, target_score_map


def _target_meta_map(target_positions: List[TargetPosition]) -> Dict[str, Dict[str, Any]]:
    meta_map: Dict[str, Dict[str, Any]] = {}
    for item in target_positions:
        raw = dict(item.raw or {})
        meta_map[str(item.symbol)] = {
            "previous_state": str(raw.get("previous_state", "") or ""),
            "desired_state": str(raw.get("desired_state", raw.get("current_state", "")) or ""),
            "current_state": str(raw.get("current_state", "") or ""),
            "desired_action": str(raw.get("desired_action", raw.get("recommended_action", "")) or ""),
            "recommended_action": str(raw.get("recommended_action", "") or ""),
            "position_action_intent": str(raw.get("position_action_intent", "") or ""),
            "mechanism_primary": str(raw.get("mechanism_primary", raw.get("preferred_mechanism", "")) or ""),
            "primary_event_type": str(raw.get("primary_event_type", "") or ""),
            "size_confidence": safe_float(raw.get("size_confidence", 0.0), 0.0),
            "target_weight_cap_v2a": safe_float(raw.get("target_weight_cap_v2a", 0.0), 0.0),
            "proposal_target_weight": safe_float(raw.get("proposal_target_weight", 0.0), 0.0),
        }
    return meta_map


def _actual_weight_map(account_state: AccountState) -> Dict[str, float]:
    nav = max(account_state.nav(), 1e-9)
    return {
        str(pos.symbol): float(pos.market_value()) / nav
        for pos in account_state.positions
    }


def _estimate_target_shares_map(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    lot_size: int,
    cash_reserve_ratio: float,
) -> Dict[str, int]:
    nav = max(account_state.nav(), 0.0)
    investable_nav = nav * max(0.0, 1.0 - cash_reserve_ratio)
    target_shares_map: Dict[str, int] = {}
    for item in target_positions:
        price = safe_float(price_map.get(item.symbol, 0.0), 0.0)
        if price <= 0:
            continue
        target_value = investable_nav * float(item.target_weight)
        raw_shares = int(target_value / price)
        target_shares_map[str(item.symbol)] = max((raw_shares // max(lot_size, 1)) * max(lot_size, 1), 0)
    return target_shares_map


def _build_symbol_rows(
    account_state: AccountState,
    target_weight_map: Dict[str, float],
    raw_target_shares_map: Dict[str, int],
    effective_target_shares_map: Dict[str, int],
    price_map: Dict[str, float],
    pending_buy_map: Dict[str, int] | None = None,
    pending_sell_map: Dict[str, int] | None = None,
    control_reason_map: Dict[str, Dict[str, Any]] | None = None,
    target_meta_map: Dict[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    current_map = _positions_to_map(account_state.positions)
    actual_weight_map = _actual_weight_map(account_state)
    pending_buy_map = dict(pending_buy_map or {})
    pending_sell_map = dict(pending_sell_map or {})
    control_reason_map = dict(control_reason_map or {})
    target_meta_map = dict(target_meta_map or {})
    nav = max(account_state.nav(), 1e-9)
    symbols = sorted(
        set(current_map.keys())
        | set(target_weight_map.keys())
        | set(raw_target_shares_map.keys())
        | set(effective_target_shares_map.keys())
        | set(pending_buy_map.keys())
        | set(pending_sell_map.keys())
    )
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        pos = current_map.get(symbol)
        actual_shares = int(pos.shares) if pos else 0
        available_shares = int(pos.available_shares) if pos else 0
        last_price = safe_float(price_map.get(symbol, pos.last_price if pos else 0.0), 0.0)
        raw_target_shares = int(raw_target_shares_map.get(symbol, 0) or 0)
        effective_target_shares = int(effective_target_shares_map.get(symbol, raw_target_shares) or 0)
        pending_buy_shares = int(pending_buy_map.get(symbol, 0) or 0)
        pending_sell_shares = int(pending_sell_map.get(symbol, 0) or 0)
        estimated_post_trade_shares = max(actual_shares + pending_buy_shares - pending_sell_shares, 0)
        target_weight = float(target_weight_map.get(symbol, 0.0) or 0.0)
        effective_target_weight_est = (effective_target_shares * last_price / nav) if last_price > 0 else 0.0
        estimated_post_trade_weight = (estimated_post_trade_shares * last_price / nav) if last_price > 0 else 0.0
        control_info = dict(control_reason_map.get(symbol, {}) or {})
        target_meta = dict(target_meta_map.get(symbol, {}) or {})
        rows.append(
            {
                "symbol": symbol,
                "actual_shares": actual_shares,
                "available_shares": available_shares,
                "raw_target_shares": raw_target_shares,
                "target_shares": effective_target_shares,
                "pending_buy_shares": pending_buy_shares,
                "pending_sell_shares": pending_sell_shares,
                "estimated_post_trade_shares": estimated_post_trade_shares,
                "actual_weight": round(actual_weight_map.get(symbol, 0.0), 6),
                "target_weight": round(target_weight, 6),
                "effective_target_weight_est": round(effective_target_weight_est, 6),
                "estimated_post_trade_weight": round(estimated_post_trade_weight, 6),
                "last_price": last_price,
                "control_action": str(control_info.get("control_action", "") or ""),
                "control_reason": str(control_info.get("control_reason", "") or ""),
                "weight_diff": round(abs(actual_weight_map.get(symbol, 0.0) - target_weight), 6),
                "previous_state": str(target_meta.get("previous_state", "") or ""),
                "desired_state": str(target_meta.get("desired_state", "") or ""),
                "current_state": str(target_meta.get("current_state", "") or ""),
                "desired_action": str(target_meta.get("desired_action", "") or ""),
                "recommended_action": str(target_meta.get("recommended_action", "") or ""),
                "position_action_intent": str(target_meta.get("position_action_intent", "") or ""),
                "mechanism_primary": str(target_meta.get("mechanism_primary", "") or ""),
                "primary_event_type": str(target_meta.get("primary_event_type", "") or ""),
                "size_confidence": round(float(target_meta.get("size_confidence", 0.0) or 0.0), 6),
                "target_weight_cap_v2a": round(float(target_meta.get("target_weight_cap_v2a", 0.0) or 0.0), 6),
                "proposal_target_weight": round(float(target_meta.get("proposal_target_weight", 0.0) or 0.0), 6),
            }
        )
    return rows


def _build_position_state_payload(
    stage: str,
    account_state: AccountState,
    target_weight_map: Dict[str, float],
    raw_target_shares_map: Dict[str, int],
    effective_target_shares_map: Dict[str, int],
    price_map: Dict[str, float],
    control_cfg: Dict[str, Any],
    pending_buy_map: Dict[str, int] | None = None,
    pending_sell_map: Dict[str, int] | None = None,
    control_reason_map: Dict[str, Dict[str, Any]] | None = None,
    target_meta_map: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    rows = _build_symbol_rows(
        account_state=account_state,
        target_weight_map=target_weight_map,
        raw_target_shares_map=raw_target_shares_map,
        effective_target_shares_map=effective_target_shares_map,
        price_map=price_map,
        pending_buy_map=pending_buy_map,
        pending_sell_map=pending_sell_map,
        control_reason_map=control_reason_map,
        target_meta_map=target_meta_map,
    )
    return {
        "generated_at": _now_text(),
        "stage": stage,
        "account_id": str(account_state.account_id),
        "account_nav": float(account_state.nav()),
        "account_cash": float(account_state.cash),
        "n_symbols": len(rows),
        "control_config": {
            "enabled": bool(control_cfg.get("enabled", True)),
            "drift_threshold": float(control_cfg.get("drift_threshold", 0.0)),
            "max_daily_turnover_ratio": float(control_cfg.get("max_daily_turnover_ratio", 0.0)),
        },
        "positions": rows,
    }


def _apply_drift_threshold(
    account_state: AccountState,
    target_weight_map: Dict[str, float],
    raw_target_shares_map: Dict[str, int],
    price_map: Dict[str, float],
    drift_threshold: float,
) -> Tuple[Dict[str, int], Dict[str, Dict[str, Any]]]:
    current_map = _positions_to_map(account_state.positions)
    actual_weight_map = _actual_weight_map(account_state)
    effective_target_shares_map = dict(raw_target_shares_map)
    control_reason_map: Dict[str, Dict[str, Any]] = {}
    symbols = set(current_map.keys()) | set(raw_target_shares_map.keys()) | set(target_weight_map.keys())
    for symbol in symbols:
        actual_shares = int(current_map.get(symbol).shares) if symbol in current_map else 0
        raw_target_shares = int(raw_target_shares_map.get(symbol, 0) or 0)
        target_weight = float(target_weight_map.get(symbol, 0.0) or 0.0)
        actual_weight = float(actual_weight_map.get(symbol, 0.0) or 0.0)
        weight_diff = abs(actual_weight - target_weight)
        is_new_entry = actual_shares <= 0 and raw_target_shares > 0
        is_full_exit = actual_shares > 0 and raw_target_shares <= 0
        if actual_shares > 0 and raw_target_shares > 0 and not is_new_entry and not is_full_exit and weight_diff < drift_threshold:
            effective_target_shares_map[symbol] = actual_shares
            control_reason_map[symbol] = {
                "control_action": "skip_drift",
                "control_reason": f"weight_diff={weight_diff:.6f} below drift_threshold={drift_threshold:.6f}",
            }
        else:
            control_reason_map[symbol] = {
                "control_action": "rebalance",
                "control_reason": (
                    "new_entry"
                    if is_new_entry
                    else ("full_exit" if is_full_exit else f"weight_diff={weight_diff:.6f}")
                ),
            }
    return effective_target_shares_map, control_reason_map


def _append_sell_orders(
    symbol: str,
    target_shares: int,
    delta_to_sell: int,
    ref_price: float,
    lot_size: int,
    allow_odd_lot_exit: bool,
    orders: List[OrderIntent],
) -> None:
    if delta_to_sell <= 0:
        return
    lot_part = (delta_to_sell // max(lot_size, 1)) * max(lot_size, 1)
    odd_part = delta_to_sell - lot_part
    if lot_part > 0:
        orders.append(
            OrderIntent(
                symbol=symbol,
                side="SELL",
                target_shares=target_shares,
                delta_shares=lot_part,
                ref_price=ref_price,
                reason="rebalance_to_target_weight",
            )
        )
    if allow_odd_lot_exit and odd_part > 0:
        orders.append(
            OrderIntent(
                symbol=symbol,
                side="SELL",
                target_shares=target_shares,
                delta_shares=odd_part,
                ref_price=ref_price,
                reason="rebalance_odd_lot_exit",
            )
        )


def _build_order_intents_from_target_shares(
    account_state: AccountState,
    effective_target_shares_map: Dict[str, int],
    price_map: Dict[str, float],
    lot_size: int,
    min_trade_value: float,
    sell_by_available: bool,
    allow_odd_lot_exit: bool,
) -> List[OrderIntent]:
    current_map = _positions_to_map(account_state.positions)
    all_symbols = set(current_map.keys()) | set(effective_target_shares_map.keys())
    sell_orders: List[OrderIntent] = []
    buy_orders: List[OrderIntent] = []
    for symbol in sorted(all_symbols):
        current_pos = current_map.get(symbol)
        current_shares = int(current_pos.shares) if current_pos else 0
        current_sellable = int(current_pos.available_shares) if current_pos else 0
        target_shares = int(effective_target_shares_map.get(symbol, 0) or 0)
        delta = target_shares - current_shares
        if delta == 0:
            continue
        ref_price = safe_float(price_map.get(symbol, current_pos.last_price if current_pos else 0.0), 0.0)
        if ref_price <= 0:
            continue
        if delta < 0:
            need_sell = abs(delta)
            if sell_by_available:
                need_sell = min(need_sell, current_sellable)
            if need_sell <= 0:
                continue
            lot_part = (need_sell // max(lot_size, 1)) * max(lot_size, 1)
            odd_part = need_sell - lot_part
            if lot_part > 0:
                notional = lot_part * ref_price
                if notional >= min_trade_value:
                    sell_orders.append(
                        OrderIntent(
                            symbol=symbol,
                            side="SELL",
                            target_shares=target_shares,
                            delta_shares=lot_part,
                            ref_price=ref_price,
                            reason="rebalance_to_target_weight",
                        )
                    )
            if allow_odd_lot_exit and odd_part > 0:
                odd_notional = odd_part * ref_price
                if odd_notional >= min_trade_value or (current_sellable <= lot_size and target_shares == 0):
                    sell_orders.append(
                        OrderIntent(
                            symbol=symbol,
                            side="SELL",
                            target_shares=target_shares,
                            delta_shares=odd_part,
                            ref_price=ref_price,
                            reason="rebalance_odd_lot_exit",
                        )
                    )
        else:
            buy_shares = (delta // max(lot_size, 1)) * max(lot_size, 1)
            if buy_shares <= 0:
                continue
            notional = buy_shares * ref_price
            if notional < min_trade_value:
                continue
            buy_orders.append(
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    target_shares=target_shares,
                    delta_shares=buy_shares,
                    ref_price=ref_price,
                    reason="rebalance_to_target_weight",
                )
            )
    return sell_orders + buy_orders


def _order_priority(
    order: OrderIntent,
    current_map: Dict[str, Position],
    target_score_map: Dict[str, float],
    control_cfg: Dict[str, Any],
) -> float:
    current_shares = int(current_map.get(order.symbol).shares) if order.symbol in current_map else 0
    base = 100.0
    if order.side == "SELL" and order.target_shares <= 0:
        base = 400.0
    elif order.side == "SELL":
        base = 320.0
    elif order.side == "BUY" and current_shares <= 0:
        base = 260.0
    elif order.side == "BUY":
        base = 200.0
    favored = {str(item).strip().upper() for item in list(control_cfg.get("llm_favored_symbols", []) or []) if str(item).strip()}
    favored_boost = float(control_cfg.get("llm_favored_score_boost", 0.0) or 0.0) if str(order.symbol).upper() in favored else 0.0
    return base + abs(order.notional()) / 100000.0 + float(target_score_map.get(order.symbol, 0.0) or 0.0) + favored_boost


def _scaled_order(order: OrderIntent, allowed_shares: int) -> OrderIntent | None:
    if allowed_shares <= 0:
        return None
    return OrderIntent(
        symbol=order.symbol,
        side=order.side,
        target_shares=order.target_shares,
        delta_shares=int(allowed_shares),
        ref_price=order.ref_price,
        reason=f"{order.reason}|turnover_scaled",
    )


def _bootstrap_diversify_buy_orders(
    orders: List[OrderIntent],
    account_state: AccountState,
    target_score_map: Dict[str, float],
    budget_value: float,
    lot_size: int,
    control_cfg: Dict[str, Any],
) -> Tuple[List[OrderIntent], List[Dict[str, Any]]]:
    if not bool(control_cfg.get("bootstrap_diversification_enabled", True)):
        return list(orders), []
    nav = max(account_state.nav(), 1e-9)
    current_exposure_ratio = max((nav - float(account_state.cash)) / nav, 0.0)
    if current_exposure_ratio > float(control_cfg.get("bootstrap_max_current_exposure_ratio", 0.05) or 0.05):
        return list(orders), []
    buy_orders = [order for order in orders if order.side == "BUY"]
    if len(buy_orders) <= 1:
        return list(orders), []
    slot_count = min(len(buy_orders), int(control_cfg.get("bootstrap_min_names", 5) or 5))
    if slot_count <= 1:
        return list(orders), []
    slot_budget = (budget_value * float(control_cfg.get("bootstrap_slot_budget_ratio", 0.9) or 0.9)) / max(slot_count, 1)
    if slot_budget <= 0:
        return list(orders), []
    ranked_buys = sorted(buy_orders, key=lambda item: float(target_score_map.get(item.symbol, 0.0) or 0.0), reverse=True)
    selected_symbols = {order.symbol for order in ranked_buys[:slot_count]}
    out: List[OrderIntent] = []
    adjustments: List[Dict[str, Any]] = []
    share_unit = max(lot_size, 1)
    for order in orders:
        if order.side != "BUY" or order.symbol not in selected_symbols:
            out.append(order)
            continue
        allowed_shares = int(slot_budget / max(order.ref_price, 1e-9))
        allowed_shares = (allowed_shares // share_unit) * share_unit
        if allowed_shares <= 0:
            adjustments.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_bootstrap_diversification",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "bootstrap_slot_budget_too_small",
                }
            )
            continue
        if allowed_shares >= int(order.delta_shares):
            out.append(order)
            continue
        scaled = _scaled_order(order, allowed_shares=allowed_shares)
        if scaled is None:
            adjustments.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_bootstrap_diversification",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "bootstrap_scaled_order_invalid",
                }
            )
            continue
        out.append(scaled)
        adjustments.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "control_action": "scale_bootstrap_diversification",
                "planned_shares": int(order.delta_shares),
                "final_shares": int(scaled.delta_shares),
                "reason": f"scaled_to_seed_{slot_count}_names",
            }
        )
    return out, adjustments


def _apply_turnover_budget(
    orders: List[OrderIntent],
    account_state: AccountState,
    target_score_map: Dict[str, float],
    max_daily_turnover_ratio: float,
    lot_size: int,
    allow_odd_lot_exit: bool,
    control_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    nav = max(account_state.nav(), 1e-9)
    budget_value = nav * max_daily_turnover_ratio
    raw_turnover_value = sum(order.notional() for order in orders)
    current_map = _positions_to_map(account_state.positions)
    ranked = sorted(
        orders,
        key=lambda item: _order_priority(
            item,
            current_map=current_map,
            target_score_map=target_score_map,
            control_cfg=control_cfg,
        ),
        reverse=True,
    )
    ranked, bootstrap_adjustments = _bootstrap_diversify_buy_orders(
        orders=ranked,
        account_state=account_state,
        target_score_map=target_score_map,
        budget_value=budget_value,
        lot_size=lot_size,
        control_cfg=control_cfg,
    )
    selected_orders: List[OrderIntent] = []
    truncated_orders: List[Dict[str, Any]] = []
    used_value = 0.0
    for order in ranked:
        order_value = order.notional()
        remaining_value = budget_value - used_value
        if remaining_value <= 0:
            truncated_orders.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_turnover_budget",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "daily_turnover_budget_exhausted",
                }
            )
            continue
        if used_value + order_value <= budget_value + 1e-9:
            selected_orders.append(order)
            used_value += order_value
            continue
        share_unit = 1 if order.side == "SELL" and allow_odd_lot_exit and order.delta_shares < lot_size else max(lot_size, 1)
        allowed_shares = int(remaining_value / max(order.ref_price, 1e-9))
        if share_unit > 1:
            allowed_shares = (allowed_shares // share_unit) * share_unit
        if allowed_shares <= 0:
            truncated_orders.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_turnover_budget",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "remaining_budget_too_small",
                }
            )
            continue
        scaled = _scaled_order(order, allowed_shares=allowed_shares)
        if scaled is None or scaled.notional() <= 0:
            truncated_orders.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_turnover_budget",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "scaled_order_invalid",
                }
            )
            continue
        selected_orders.append(scaled)
        used_value += scaled.notional()
        truncated_orders.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "control_action": "scale_turnover_budget",
                "planned_shares": int(order.delta_shares),
                "final_shares": int(scaled.delta_shares),
                "reason": "scaled_to_fit_daily_turnover_budget",
            }
        )
    return {
        "budget_value": budget_value,
        "raw_turnover_value": raw_turnover_value,
        "final_turnover_value": used_value,
        "raw_turnover_ratio": raw_turnover_value / nav,
        "final_turnover_ratio": used_value / nav,
        "selected_orders": selected_orders,
        "truncated_orders": list(bootstrap_adjustments) + truncated_orders,
    }


def _apply_small_account_buy_slicing(
    orders: List[OrderIntent],
    account_state: AccountState,
    lot_size: int,
    min_trade_value: float,
    control_cfg: Dict[str, Any],
) -> Tuple[List[OrderIntent], List[Dict[str, Any]]]:
    if not bool(control_cfg.get("small_account_slicing_enabled", True)):
        return list(orders), []
    nav = max(float(account_state.nav()), 0.0)
    if nav <= 0 or nav > float(control_cfg.get("small_account_nav_threshold", 50000.0) or 50000.0):
        return list(orders), []
    cash = max(float(account_state.cash), 0.0)
    per_order_cap = min(
        nav * float(control_cfg.get("small_account_max_single_buy_nav_ratio", 0.22) or 0.22),
        cash * float(control_cfg.get("small_account_max_single_buy_cash_ratio", 0.28) or 0.28),
    )
    if per_order_cap <= 0:
        return list(orders), []
    share_unit = max(lot_size, 1)
    adjusted: List[OrderIntent] = []
    notes: List[Dict[str, Any]] = []
    for order in list(orders or []):
        if order.side != "BUY":
            adjusted.append(order)
            continue
        if order.notional() <= per_order_cap + 1e-9:
            adjusted.append(order)
            continue
        allowed_shares = int(per_order_cap / max(order.ref_price, 1e-9))
        allowed_shares = (allowed_shares // share_unit) * share_unit
        if allowed_shares <= 0 or allowed_shares * order.ref_price < min_trade_value:
            notes.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_small_account_slice",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "small_account_per_order_cap_below_min_trade_value",
                }
            )
            continue
        scaled = _scaled_order(order, allowed_shares=allowed_shares)
        if scaled is None:
            notes.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "control_action": "skip_small_account_slice",
                    "planned_shares": int(order.delta_shares),
                    "final_shares": 0,
                    "reason": "small_account_scaled_order_invalid",
                }
            )
            continue
        adjusted.append(scaled)
        notes.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "control_action": "scale_small_account_slice",
                "planned_shares": int(order.delta_shares),
                "final_shares": int(scaled.delta_shares),
                "reason": f"per_order_cap={per_order_cap:.2f}",
            }
        )
    return adjusted, notes


def _apply_llm_symbol_blocks(
    orders: List[OrderIntent],
    control_cfg: Dict[str, Any],
) -> Tuple[List[OrderIntent], List[Dict[str, Any]]]:
    blocked = {str(item).strip().upper() for item in list(control_cfg.get("llm_blocked_symbols", []) or []) if str(item).strip()}
    if not blocked:
        return list(orders), []
    allowed: List[OrderIntent] = []
    notes: List[Dict[str, Any]] = []
    for order in list(orders or []):
        if order.side != "BUY" or str(order.symbol).upper() not in blocked:
            allowed.append(order)
            continue
        notes.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "control_action": "skip_llm_review",
                "planned_shares": int(order.delta_shares),
                "final_shares": 0,
                "reason": "llm_review_blocked_symbol",
            }
        )
    return allowed, notes


def _apply_preferred_t_mechanism(
    orders: List[OrderIntent],
    control_cfg: Dict[str, Any],
    target_meta_map: Dict[str, Dict[str, Any]],
) -> Tuple[List[OrderIntent], List[Dict[str, Any]]]:
    preferred = str(control_cfg.get("preferred_t_mechanism", "") or "").strip()
    if not preferred or not bool(control_cfg.get("preferred_t_mechanism_enforced", False)):
        return list(orders), []
    buy_only = bool(control_cfg.get("preferred_t_mechanism_buy_only", True))
    allowed: List[OrderIntent] = []
    blocked: List[Dict[str, Any]] = []
    for order in list(orders or []):
        if buy_only and order.side != "BUY":
            allowed.append(order)
            continue
        meta = dict(target_meta_map.get(order.symbol, {}) or {})
        actual = str(meta.get("mechanism_primary", "") or "").strip()
        if actual == preferred:
            allowed.append(order)
            continue
        blocked.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "control_action": "skip_preferred_t_mechanism",
                "planned_shares": int(order.delta_shares),
                "final_shares": 0,
                "reason": f"preferred_t_mechanism={preferred}; actual_mechanism={actual or 'unlabeled'}",
            }
        )
    return allowed, blocked


def plan_portfolio_control(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    lot_size: int,
    min_trade_value: float,
    cash_reserve_ratio: float,
    sell_by_available: bool,
    control_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    control_cfg, dynamic_account_summary = _derive_dynamic_account_control(
        account_state=account_state,
        target_positions=target_positions,
        price_map=price_map,
        lot_size=lot_size,
        min_trade_value=min_trade_value,
        control_cfg=control_cfg,
    )
    target_weight_map, target_score_map = _target_maps(target_positions)
    target_meta_map = _target_meta_map(target_positions)
    raw_target_shares_map = _estimate_target_shares_map(
        account_state=account_state,
        target_positions=target_positions,
        price_map=price_map,
        lot_size=lot_size,
        cash_reserve_ratio=cash_reserve_ratio,
    )
    effective_target_shares_map, control_reason_map = _apply_drift_threshold(
        account_state=account_state,
        target_weight_map=target_weight_map,
        raw_target_shares_map=raw_target_shares_map,
        price_map=price_map,
        drift_threshold=float(control_cfg.get("drift_threshold", 0.0)),
    )
    raw_orders = _build_order_intents_from_target_shares(
        account_state=account_state,
        effective_target_shares_map=effective_target_shares_map,
        price_map=price_map,
        lot_size=lot_size,
        min_trade_value=min_trade_value,
        sell_by_available=sell_by_available,
        allow_odd_lot_exit=bool(control_cfg.get("allow_odd_lot_exit", True)),
    )
    budget_result = _apply_turnover_budget(
        orders=raw_orders,
        account_state=account_state,
        target_score_map=target_score_map,
        max_daily_turnover_ratio=float(control_cfg.get("max_daily_turnover_ratio", 0.0)),
        lot_size=lot_size,
        allow_odd_lot_exit=bool(control_cfg.get("allow_odd_lot_exit", True)),
        control_cfg=control_cfg,
    )
    final_orders: List[OrderIntent] = list(budget_result["selected_orders"])
    small_account_adjustments: List[Dict[str, Any]] = []
    final_orders, small_account_adjustments = _apply_small_account_buy_slicing(
        orders=final_orders,
        account_state=account_state,
        lot_size=lot_size,
        min_trade_value=min_trade_value,
        control_cfg=control_cfg,
    )
    llm_blocked_orders: List[Dict[str, Any]] = []
    final_orders, llm_blocked_orders = _apply_llm_symbol_blocks(
        orders=final_orders,
        control_cfg=control_cfg,
    )
    reduce_only_blocked_orders: List[Dict[str, Any]] = []
    if bool(control_cfg.get("reduce_only", False)):
        for order in final_orders:
            if order.side == "BUY":
                reduce_only_blocked_orders.append(
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "control_action": "skip_reduce_only",
                        "planned_shares": int(order.delta_shares),
                        "final_shares": 0,
                        "reason": "reduce_only_mode",
                    }
                )
        final_orders = [order for order in final_orders if order.side != "BUY"]
    preferred_mechanism_blocked_orders: List[Dict[str, Any]] = []
    final_orders, preferred_mechanism_blocked_orders = _apply_preferred_t_mechanism(
        orders=final_orders,
        control_cfg=control_cfg,
        target_meta_map=target_meta_map,
    )
    pending_buy_map: Dict[str, int] = {}
    pending_sell_map: Dict[str, int] = {}
    for order in final_orders:
        if order.side == "BUY":
            pending_buy_map[order.symbol] = int(pending_buy_map.get(order.symbol, 0) or 0) + int(order.delta_shares)
        else:
            pending_sell_map[order.symbol] = int(pending_sell_map.get(order.symbol, 0) or 0) + int(order.delta_shares)

    before_state = _build_position_state_payload(
        stage="before_plan",
        account_state=account_state,
        target_weight_map=target_weight_map,
        raw_target_shares_map=raw_target_shares_map,
        effective_target_shares_map=effective_target_shares_map,
        price_map=price_map,
        control_cfg=control_cfg,
        control_reason_map=control_reason_map,
        target_meta_map=target_meta_map,
    )
    after_plan_state = _build_position_state_payload(
        stage="after_plan",
        account_state=account_state,
        target_weight_map=target_weight_map,
        raw_target_shares_map=raw_target_shares_map,
        effective_target_shares_map=effective_target_shares_map,
        price_map=price_map,
        control_cfg=control_cfg,
        pending_buy_map=pending_buy_map,
        pending_sell_map=pending_sell_map,
        control_reason_map=control_reason_map,
        target_meta_map=target_meta_map,
    )
    drift_skipped = sum(1 for item in control_reason_map.values() if item.get("control_action") == "skip_drift")
    audit = {
        "generated_at": _now_text(),
        "control_config": {
            "drift_threshold": float(control_cfg.get("drift_threshold", 0.0)),
            "max_daily_turnover_ratio": float(control_cfg.get("max_daily_turnover_ratio", 0.0)),
            "dynamic_account_scaling_enabled": bool(control_cfg.get("dynamic_account_scaling_enabled", True)),
            "dynamic_account_max_turnover_ratio": float(control_cfg.get("dynamic_account_max_turnover_ratio", 0.0)),
            "allow_odd_lot_exit": bool(control_cfg.get("allow_odd_lot_exit", True)),
            "small_account_slicing_enabled": bool(control_cfg.get("small_account_slicing_enabled", True)),
            "small_account_nav_threshold": float(control_cfg.get("small_account_nav_threshold", 0.0)),
            "small_account_max_single_buy_nav_ratio": float(control_cfg.get("small_account_max_single_buy_nav_ratio", 0.0)),
            "small_account_max_single_buy_cash_ratio": float(control_cfg.get("small_account_max_single_buy_cash_ratio", 0.0)),
            "reduce_only": bool(control_cfg.get("reduce_only", False)),
            "preferred_t_mechanism": str(control_cfg.get("preferred_t_mechanism", "") or ""),
            "preferred_t_mechanism_enforced": bool(control_cfg.get("preferred_t_mechanism_enforced", False)),
            "preferred_t_mechanism_buy_only": bool(control_cfg.get("preferred_t_mechanism_buy_only", True)),
            "llm_blocked_symbols": list(control_cfg.get("llm_blocked_symbols", []) or []),
            "llm_favored_symbols": list(control_cfg.get("llm_favored_symbols", []) or []),
        },
        "account_nav": float(account_state.nav()),
        "raw_turnover_value": float(budget_result["raw_turnover_value"]),
        "raw_turnover_ratio": float(budget_result["raw_turnover_ratio"]),
        "turnover_budget_value": float(budget_result["budget_value"]),
        "final_turnover_value": float(sum(order.notional() for order in final_orders)),
        "final_turnover_ratio": float(sum(order.notional() for order in final_orders) / max(account_state.nav(), 1e-9)),
        "n_raw_orders": len(raw_orders),
        "n_final_orders": len(final_orders),
        "n_drift_skipped_symbols": drift_skipped,
        "n_turnover_adjustments": len(list(budget_result.get("truncated_orders", []) or [])) + len(small_account_adjustments) + len(llm_blocked_orders) + len(reduce_only_blocked_orders) + len(preferred_mechanism_blocked_orders),
        "turnover_adjustments": list(budget_result.get("truncated_orders", []) or []) + small_account_adjustments + llm_blocked_orders + reduce_only_blocked_orders + preferred_mechanism_blocked_orders,
        "dynamic_account_summary": dynamic_account_summary,
        "symbol_controls": before_state["positions"],
    }
    return {
        "control_config": control_cfg,
        "target_weight_map": target_weight_map,
        "target_meta_map": target_meta_map,
        "raw_target_shares_map": raw_target_shares_map,
        "effective_target_shares_map": effective_target_shares_map,
        "raw_orders": raw_orders,
        "final_orders": final_orders,
        "position_state_before": before_state,
        "position_state_after_plan": after_plan_state,
        "rebalance_audit": audit,
        "summary": {
            "n_drift_skipped_symbols": drift_skipped,
            "raw_turnover_ratio": float(budget_result["raw_turnover_ratio"]),
            "final_turnover_ratio": float(sum(order.notional() for order in final_orders) / max(account_state.nav(), 1e-9)),
            "n_turnover_adjustments": len(list(budget_result.get("truncated_orders", []) or [])) + len(small_account_adjustments) + len(llm_blocked_orders) + len(reduce_only_blocked_orders) + len(preferred_mechanism_blocked_orders),
            "n_reduce_only_blocked_orders": len(reduce_only_blocked_orders),
            "n_preferred_t_mechanism_blocked_orders": len(preferred_mechanism_blocked_orders),
            "n_llm_blocked_orders": len(llm_blocked_orders),
            "n_small_account_sliced_orders": len(small_account_adjustments),
            "dynamic_account_affordable_names": int(dynamic_account_summary.get("affordable_names", 0) or 0),
        },
    }


def _pending_share_maps(unfinished_orders: List[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, int]]:
    pending_buy_map: Dict[str, int] = {}
    pending_sell_map: Dict[str, int] = {}
    for row in unfinished_orders:
        symbol = str(row.get("symbol", "") or "").strip()
        if not symbol:
            continue
        remaining = max(int(row.get("remaining_shares", 0) or 0), 0)
        if remaining <= 0:
            continue
        if str(row.get("side", "")).upper() == "BUY":
            pending_buy_map[symbol] = int(pending_buy_map.get(symbol, 0) or 0) + remaining
        else:
            pending_sell_map[symbol] = int(pending_sell_map.get(symbol, 0) or 0) + remaining
    return pending_buy_map, pending_sell_map


def build_execution_feedback(
    planned_orders: List[OrderIntent],
    skipped_actions: List[Dict[str, Any]],
    fills: List[FillRecord],
    submitted_orders: List[Dict[str, Any]],
    day_orders: List[Dict[str, Any]],
    unfinished_orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fill_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for fill in fills:
        key = (str(fill.order_id or ""), str(fill.symbol))
        bucket = fill_map.setdefault(key, {"filled_shares": 0, "gross_amount": 0.0, "fee": 0.0})
        bucket["filled_shares"] += int(fill.shares)
        bucket["gross_amount"] += float(fill.gross_amount)
        bucket["fee"] += float(fill.fee)

    day_order_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in day_orders:
        key = (str(row.get("order_id", "") or ""), str(row.get("symbol", "") or ""))
        day_order_map[key] = row

    unfinished_key_set = {
        (str(row.get("order_id", "") or ""), str(row.get("symbol", "") or ""))
        for row in unfinished_orders
    }

    order_rows: List[Dict[str, Any]] = []
    for idx, order in enumerate(planned_orders, start=1):
        submitted = submitted_orders[idx - 1] if idx - 1 < len(submitted_orders) else {}
        order_id = str(submitted.get("order_id", "") or "")
        symbol = str(order.symbol)
        key = (order_id, symbol)
        fill_bucket = dict(fill_map.get(key, {}) or {})
        day_row = dict(day_order_map.get(key, {}) or {})
        planned_shares = int(order.delta_shares)
        submitted_shares = int(submitted.get("delta_shares", planned_shares) or planned_shares)
        filled_shares = int(fill_bucket.get("filled_shares", day_row.get("filled_volume", 0)) or 0)
        if key in unfinished_key_set and filled_shares < submitted_shares:
            status = "partial"
            reason = "unfinished_order_pending"
        elif filled_shares >= submitted_shares > 0:
            status = "success"
            reason = "filled"
        elif filled_shares > 0:
            status = "partial"
            reason = "partial_fill"
        elif str(day_row.get("status_name", "")).lower() in {"rejected", "canceled", "expired"}:
            status = "failed"
            reason = str(day_row.get("status_detail") or day_row.get("status_name") or "order_not_filled")
        else:
            status = "failed"
            reason = str(day_row.get("status_detail") or "no_fill_after_wait_window")
        order_rows.append(
            {
                "symbol": symbol,
                "planned_action": order.side,
                "planned_shares": planned_shares,
                "submitted_shares": submitted_shares,
                "filled_shares": filled_shares,
                "status": status,
                "reason": reason[:240],
                "order_id": order_id,
                "cl_ord_id": str(submitted.get("cl_ord_id", "") or ""),
                "submit_price": safe_float(submitted.get("submit_price", order.ref_price), order.ref_price),
                "filled_amount": float(fill_bucket.get("gross_amount", 0.0) or 0.0),
                "fee": float(fill_bucket.get("fee", 0.0) or 0.0),
            }
        )

    for action in skipped_actions:
        order_rows.append(
            {
                "symbol": str(action.get("symbol", "") or ""),
                "planned_action": str(action.get("side", "") or ""),
                "planned_shares": int(action.get("planned_shares", 0) or 0),
                "submitted_shares": int(action.get("final_shares", 0) or 0),
                "filled_shares": 0,
                "status": "skipped",
                "reason": str(action.get("reason", "") or "")[:240],
                "order_id": "",
                "cl_ord_id": "",
                "submit_price": 0.0,
                "filled_amount": 0.0,
                "fee": 0.0,
            }
        )

    summary = {
        "n_success": sum(row["status"] == "success" for row in order_rows),
        "n_partial": sum(row["status"] == "partial" for row in order_rows),
        "n_failed": sum(row["status"] == "failed" for row in order_rows),
        "n_skipped": sum(row["status"] == "skipped" for row in order_rows),
    }
    return {
        "generated_at": _now_text(),
        "summary": summary,
        "orders": order_rows,
        "unfinished_orders": unfinished_orders,
    }


def write_portfolio_control_artifacts(
    output_dir: str | Path,
    timestamp: str,
    position_state_before: Dict[str, Any],
    position_state_after_plan: Dict[str, Any],
    position_state_after_execution: Dict[str, Any],
    rebalance_audit: Dict[str, Any],
    execution_feedback: Dict[str, Any],
) -> Dict[str, str]:
    root = ensure_dir(Path(output_dir) / "portfolio_control_runs" / timestamp)
    before_path = root / "position_state_before.json"
    after_plan_path = root / "position_state_after_plan.json"
    after_execution_path = root / "position_state_after_execution.json"
    audit_path = root / "rebalance_audit.json"
    feedback_path = root / "execution_feedback.json"
    dump_json(before_path, position_state_before)
    dump_json(after_plan_path, position_state_after_plan)
    dump_json(after_execution_path, position_state_after_execution)
    dump_json(audit_path, rebalance_audit)
    dump_json(feedback_path, execution_feedback)

    latest_root = ensure_dir(Path(output_dir))
    dump_json(latest_root / "latest_position_state_before.json", position_state_before)
    dump_json(latest_root / "latest_position_state_after_plan.json", position_state_after_plan)
    dump_json(latest_root / "latest_position_state_after_execution.json", position_state_after_execution)
    dump_json(latest_root / "latest_rebalance_audit.json", rebalance_audit)
    dump_json(latest_root / "latest_execution_feedback.json", execution_feedback)

    return {
        "run_dir": str(root),
        "position_state_before": str(before_path),
        "position_state_after_plan": str(after_plan_path),
        "position_state_after_execution": str(after_execution_path),
        "rebalance_audit": str(audit_path),
        "execution_feedback": str(feedback_path),
    }
