from __future__ import annotations

from typing import Dict, List, Tuple

from ..models import AccountState, FillRecord, OrderIntent
from .base import BaseBroker


class GmTradeAdapterStub(BaseBroker):
    """掘金仿真 / gmtrade 适配器预留桩。

    Args:
        无。

    Returns:
        GmTradeAdapterStub: 占位适配器对象。
    """

    def load_account_state(self) -> AccountState:
        """加载账户状态。

        Args:
            无。

        Returns:
            AccountState: 当前账户状态。
        """
        raise NotImplementedError("gmtrade 适配器建议在你选定仿真账户后再按实际 API 字段接。")

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
        raise NotImplementedError("gmtrade 适配器建议在你选定仿真账户后再按实际 API 字段接。")
