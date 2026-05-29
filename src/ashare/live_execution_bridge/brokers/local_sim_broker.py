from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from ..models import AccountState, FillRecord, Position, OrderIntent
from ..utils import dump_json, load_json
from .base import BaseBroker


class LocalSimBroker(BaseBroker):
    """本地模拟券商。

    Args:
        state_path: 账户状态文件。
        account_id: 账户编号。
        initial_cash: 初始现金。
        buy_fee_rate: 买入费率。
        sell_fee_rate: 卖出费率，默认已含印花税。
        slippage_bp: 滑点基点数。

    Returns:
        LocalSimBroker: 模拟券商实例。
    """

    def __init__(
        self,
        state_path: str | Path,
        account_id: str,
        initial_cash: float,
        buy_fee_rate: float,
        sell_fee_rate: float,
        slippage_bp: float,
    ) -> None:
        self.state_path = Path(state_path)
        self.account_id = account_id
        self.initial_cash = float(initial_cash)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.slippage_bp = float(slippage_bp)

    def load_account_state(self) -> AccountState:
        """加载账户状态。

        Args:
            无。

        Returns:
            AccountState: 当前账户状态。
        """
        data = load_json(self.state_path, default=None)
        if not data:
            return AccountState(account_id=self.account_id, cash=self.initial_cash)
        positions = [
            Position(
                symbol=item["symbol"],
                shares=int(item["shares"]),
                avg_cost=float(item["avg_cost"]),
                last_price=float(item.get("last_price", item["avg_cost"])),
            )
            for item in data.get("positions", [])
        ]
        return AccountState(
            account_id=data.get("account_id", self.account_id),
            cash=float(data.get("cash", self.initial_cash)),
            positions=positions,
            history=list(data.get("history", [])),
        )

    def _save_account_state(self, account_state: AccountState) -> None:
        """保存账户状态。

        Args:
            account_state: 账户状态。

        Returns:
            None: 无返回值。
        """
        dump_json(self.state_path, account_state.to_dict())

    def execute_orders(
        self,
        order_intents: List[OrderIntent],
        price_map: Dict[str, float],
    ) -> Tuple[AccountState, List[FillRecord]]:
        """按顺序执行订单。

        Args:
            order_intents: 订单意图列表。
            price_map: 最新价格映射。

        Returns:
            Tuple[AccountState, List[FillRecord]]: 更新后的账户状态与成交记录。
        """
        state = self.load_account_state()
        pos_map = {pos.symbol: pos for pos in state.positions}
        fills: List[FillRecord] = []

        for order in order_intents:
            base_price = float(price_map[order.symbol])
            slip = self.slippage_bp / 10000.0
            fill_price = base_price * (1.0 + slip if order.side == "BUY" else 1.0 - slip)
            shares = int(order.delta_shares)
            gross_amount = float(fill_price * shares)
            fee_rate = self.buy_fee_rate if order.side == "BUY" else self.sell_fee_rate
            fee = gross_amount * fee_rate

            if order.side == "SELL":
                position = pos_map.get(order.symbol)
                if position is None or position.shares < shares:
                    continue
                position.shares -= shares
                position.last_price = fill_price
                state.cash += gross_amount - fee
                if position.shares == 0:
                    pos_map.pop(order.symbol, None)
                net_cash_flow = gross_amount - fee
            else:
                total_need = gross_amount + fee
                if state.cash < total_need:
                    continue
                position = pos_map.get(order.symbol)
                if position is None:
                    pos_map[order.symbol] = Position(
                        symbol=order.symbol,
                        shares=shares,
                        avg_cost=fill_price,
                        last_price=fill_price,
                    )
                else:
                    old_cost = position.avg_cost * position.shares
                    new_cost = old_cost + gross_amount
                    position.shares += shares
                    position.avg_cost = new_cost / max(position.shares, 1)
                    position.last_price = fill_price
                state.cash -= total_need
                net_cash_flow = -total_need

            fills.append(
                FillRecord(
                    symbol=order.symbol,
                    side=order.side,
                    shares=shares,
                    price=fill_price,
                    gross_amount=gross_amount,
                    fee=fee,
                    net_cash_flow=net_cash_flow,
                )
            )

        # 用最新价格更新持仓行情。
        state.positions = []
        for symbol, position in pos_map.items():
            position.last_price = float(price_map.get(symbol, position.last_price))
            state.positions.append(position)
        self._save_account_state(state)
        return state, fills
