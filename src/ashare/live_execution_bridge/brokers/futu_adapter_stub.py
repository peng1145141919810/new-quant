from __future__ import annotations

from typing import Dict, List, Tuple

from ..models import AccountState, FillRecord, OrderIntent
from .base import BaseBroker


class FutuAdapterStub(BaseBroker):
    """富途 OpenAPI 适配器预留桩。

    Args:
        无。

    Returns:
        FutuAdapterStub: 占位适配器对象。
    """

    def load_account_state(self) -> AccountState:
        """加载账户状态。

        Args:
            无。

        Returns:
            AccountState: 当前账户状态。
        """
        raise NotImplementedError("富途适配器建议在 OpenD 与模拟账户联通后再接。")

    def execute_orders(
        self,
        order_intents: List[OrderIntent],
        price_map: Dict[str, float],
    ) -> Tuple[AccountState, List[FillRecord]]:
        """执行订单。

        Args:
            order_intents: 订单意图列表。
            price_map: 最新价格映射。

        Returns:
            Tuple[AccountState, List[FillRecord]]: 执行结果。
        """
        raise NotImplementedError("富途适配器建议在 OpenD 与模拟账户联通后再接。")
