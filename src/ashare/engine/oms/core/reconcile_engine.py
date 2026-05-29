from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pandas as pd

from live_execution_bridge.models import AccountState, Position, TargetPosition
from live_execution_bridge.utils import safe_float


def _positions_map(positions: Iterable[Position]) -> Dict[str, Position]:
    return {str(pos.symbol): pos for pos in positions}


def _actual_weight_map(account_state: AccountState) -> Dict[str, float]:
    nav = max(float(account_state.nav()), 1e-9)
    return {str(pos.symbol): float(pos.market_value()) / nav for pos in account_state.positions}


def _estimate_target_shares(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    lot_size: int,
    cash_reserve_ratio: float,
) -> Dict[str, int]:
    nav = max(float(account_state.nav()), 0.0)
    investable_nav = nav * max(0.0, 1.0 - float(cash_reserve_ratio))
    target_shares_map: Dict[str, int] = {}
    for item in target_positions:
        price = safe_float(price_map.get(item.symbol, 0.0), 0.0)
        if price <= 0:
            continue
        target_value = investable_nav * float(item.target_weight)
        raw_shares = int(target_value / price)
        target_shares_map[str(item.symbol)] = max((raw_shares // max(lot_size, 1)) * max(lot_size, 1), 0)
    return target_shares_map


def build_desired_vs_actual_gap(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    unfinished_orders: List[Dict[str, Any]],
    release_id: str,
    lot_size: int,
    cash_reserve_ratio: float,
) -> pd.DataFrame:
    actual_map = _positions_map(account_state.positions)
    actual_weight_map = _actual_weight_map(account_state)
    target_share_map = _estimate_target_shares(
        account_state=account_state,
        target_positions=target_positions,
        price_map=price_map,
        lot_size=lot_size,
        cash_reserve_ratio=cash_reserve_ratio,
    )
    target_map = {str(item.symbol): item for item in target_positions}
    pending_buy: Dict[str, int] = {}
    pending_sell: Dict[str, int] = {}
    for row in list(unfinished_orders or []):
        symbol = str(row.get("symbol", "") or "").strip()
        remaining = max(int(row.get("remaining_shares", 0) or 0), 0)
        if not symbol or remaining <= 0:
            continue
        if str(row.get("side", "")).upper() == "BUY":
            pending_buy[symbol] = int(pending_buy.get(symbol, 0) or 0) + remaining
        else:
            pending_sell[symbol] = int(pending_sell.get(symbol, 0) or 0) + remaining

    symbols = sorted(set(actual_map.keys()) | set(target_map.keys()) | set(pending_buy.keys()) | set(pending_sell.keys()))
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        pos = actual_map.get(symbol)
        target = target_map.get(symbol)
        actual_shares = int(pos.shares) if pos else 0
        available_shares = int(pos.available_shares) if pos else 0
        target_weight = float(target.target_weight) if target else 0.0
        target_shares = int(target_share_map.get(symbol, 0) or 0)
        open_buy_shares = int(pending_buy.get(symbol, 0) or 0)
        open_sell_shares = int(pending_sell.get(symbol, 0) or 0)
        effective_actual_shares = max(actual_shares + open_buy_shares - open_sell_shares, 0)
        desired_state = ""
        desired_action = ""
        mechanism_primary = ""
        if target is not None:
            raw = dict(target.raw or {})
            desired_state = str(raw.get("desired_state", raw.get("current_state", "")) or "")
            desired_action = str(raw.get("desired_action", raw.get("recommended_action", "")) or "")
            mechanism_primary = str(raw.get("mechanism_primary", "") or "")
        gap_shares = int(target_shares - effective_actual_shares)
        gap_weight = float(target_weight - actual_weight_map.get(symbol, 0.0))
        last_price = float(price_map.get(symbol, pos.last_price if pos else 0.0) or 0.0)
        rows.append(
            {
                "release_id": str(release_id or ""),
                "symbol": symbol,
                "desired_state": desired_state,
                "desired_action": desired_action,
                "mechanism_primary": mechanism_primary,
                "actual_shares": actual_shares,
                "available_shares": available_shares,
                "actual_weight": round(float(actual_weight_map.get(symbol, 0.0) or 0.0), 6),
                "target_weight": round(float(target_weight), 6),
                "last_price": round(last_price, 6),
                "target_shares": target_shares,
                "open_buy_shares": open_buy_shares,
                "open_sell_shares": open_sell_shares,
                "effective_actual_shares": effective_actual_shares,
                "gap_shares": gap_shares,
                "gap_weight": round(float(gap_weight), 6),
                "gap_weight_abs": round(abs(float(gap_weight)), 6),
                "has_desired": bool(target_weight > 0 or target_shares > 0 or desired_state),
                "has_actual": bool(actual_shares > 0),
                "target_market_value": round(float(target_shares) * last_price, 4),
                "actual_market_value": round(float(actual_shares) * last_price, 4),
            }
        )
    return pd.DataFrame(rows)
