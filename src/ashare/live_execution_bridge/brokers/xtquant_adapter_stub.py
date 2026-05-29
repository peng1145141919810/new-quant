from __future__ import annotations

from typing import Dict, List, Tuple

from ..models import AccountState, FillRecord, OrderIntent
from .base import BaseBroker


class XtQuantAdapterStub(BaseBroker):
    """QMT / MiniQMT 适配器预留桩。

    这份文件故意只留结构，不伪造具体可跑实现。
    你本地真正接时，建议在这个类里做三件事：
    1. 建立 XtQuantTrader 连接。
    2. 把账户资产与持仓映射为 AccountState。
    3. 把 OrderIntent 映射为 order_stock_async / order_stock 指令。

    Args:
        无。

    Returns:
        XtQuantAdapterStub: 占位适配器对象。
    """

    def load_account_state(self) -> AccountState:
        """加载账户状态。

        Args:
            无。

        Returns:
            AccountState: 当前账户状态。
        """
        raise NotImplementedError("QMT 适配器需要在用户本地终端环境中联调后再接。")

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
        raise NotImplementedError("QMT 适配器需要在用户本地终端环境中联调后再接。")
