from __future__ import annotations

from typing import Dict, List

from .models import AccountState, OrderIntent, Position, TargetPosition


def _positions_to_dict(positions: List[Position]) -> Dict[str, Position]:
    """把持仓列表转成映射。

    Args:
        positions: 持仓列表。

    Returns:
        Dict[str, Position]: 代码到持仓对象的映射。
    """
    return {pos.symbol: pos for pos in positions}


def plan_rebalance(
    account_state: AccountState,
    target_positions: List[TargetPosition],
    price_map: Dict[str, float],
    lot_size: int,
    min_trade_value: float,
    cash_reserve_ratio: float,
    sell_by_available: bool,
) -> List[OrderIntent]:
    """生成调仓计划。

    Args:
        account_state: 当前账户状态。
        target_positions: 目标仓位列表。
        price_map: 最新价格映射。
        lot_size: A 股最小交易单位。
        min_trade_value: 最小下单金额限制。
        cash_reserve_ratio: 现金保留比例。
        sell_by_available: 卖出时是否按可卖数量约束。

    Returns:
        List[OrderIntent]: 订单意图列表。
    """
    current_map = _positions_to_dict(account_state.positions)
    nav = account_state.nav()
    investable_nav = nav * (1.0 - cash_reserve_ratio)

    target_shares_map: Dict[str, int] = {}
    for item in target_positions:
        price = float(price_map.get(item.symbol, 0.0))
        if price <= 0:
            continue
        target_value = investable_nav * float(item.target_weight)
        raw_shares = int(target_value / price)
        rounded_shares = (raw_shares // lot_size) * lot_size
        target_shares_map[item.symbol] = max(rounded_shares, 0)

    # 当前有、目标没有的仓位要清掉。
    for symbol, pos in current_map.items():
        target_shares_map.setdefault(symbol, 0)
        price_map.setdefault(symbol, pos.last_price)

    sell_orders: List[OrderIntent] = []
    buy_orders: List[OrderIntent] = []
    for symbol, target_shares in target_shares_map.items():
        current_pos = current_map.get(symbol)
        current_shares = int(current_pos.shares) if current_pos else 0
        current_sellable = int(current_pos.available_shares) if current_pos else 0
        delta = int(target_shares - current_shares)
        if delta == 0:
            continue
        ref_price = float(price_map[symbol])

        if delta < 0:
            need_sell = abs(delta)
            if sell_by_available:
                need_sell = min(need_sell, current_sellable)
            need_sell = (need_sell // lot_size) * lot_size
            if need_sell <= 0:
                continue
            notional = need_sell * ref_price
            if notional < min_trade_value:
                continue
            sell_orders.append(
                OrderIntent(
                    symbol=symbol,
                    side="SELL",
                    target_shares=target_shares,
                    delta_shares=need_sell,
                    ref_price=ref_price,
                    reason="rebalance_to_target_weight",
                )
            )
        else:
            delta = (delta // lot_size) * lot_size
            if delta <= 0:
                continue
            notional = delta * ref_price
            if notional < min_trade_value:
                continue
            buy_orders.append(
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    target_shares=target_shares,
                    delta_shares=delta,
                    ref_price=ref_price,
                    reason="rebalance_to_target_weight",
                )
            )
    return sell_orders + buy_orders
