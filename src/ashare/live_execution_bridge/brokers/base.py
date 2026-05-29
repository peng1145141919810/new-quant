from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from ..models import AccountState, FillRecord, OrderIntent


class BaseBroker(ABC):
    """券商适配器抽象基类。"""

    @abstractmethod
    def load_account_state(self) -> AccountState:
        """加载账户状态。

        Args:
            None

        Returns:
            AccountState: 当前账户状态。
        """
        raise NotImplementedError

    @abstractmethod
    def execute_orders(
        self,
        order_intents: List[OrderIntent],
        price_map: Dict[str, float],
    ) -> Tuple[AccountState, List[FillRecord], List[dict]]:
        """执行订单。

        Args:
            order_intents: 订单意图列表。
            price_map: 最新价格映射。

        Returns:
            Tuple[AccountState, List[FillRecord], List[dict]]: 执行后的账户状态、成交记录和原始委托摘要。
        """
        raise NotImplementedError

    def cancel_orders(self, order_rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """可选的撤单接口。默认不做任何事。"""
        return []
