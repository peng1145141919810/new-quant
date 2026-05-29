from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class TargetPosition:
    """目标仓位对象。

    Args:
        symbol: 证券代码，内部统一为 600000.SH / 000001.SZ 形式。
        target_weight: 目标权重。
        score: 模型分数，可为空。
        raw: 原始行信息。

    Returns:
        TargetPosition: 目标仓位数据对象。
    """

    symbol: str
    target_weight: float
    score: float | None = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。

        Args:
            None

        Returns:
            Dict[str, Any]: 可序列化字典。
        """
        return asdict(self)


@dataclass
class Position:
    """当前持仓对象。

    Args:
        symbol: 证券代码。
        shares: 当前持股数量。
        avg_cost: 持仓成本价。
        last_price: 最新价。
        available_shares: 当前可卖数量。

    Returns:
        Position: 当前持仓对象。
    """

    symbol: str
    shares: int
    avg_cost: float
    last_price: float
    available_shares: int = 0

    def market_value(self) -> float:
        """计算持仓市值。

        Args:
            None

        Returns:
            float: 当前持仓市值。
        """
        return float(self.shares) * float(self.last_price)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。

        Args:
            None

        Returns:
            Dict[str, Any]: 可序列化字典。
        """
        return asdict(self)


@dataclass
class OrderIntent:
    """调仓指令对象。

    Args:
        symbol: 证券代码。
        side: BUY 或 SELL。
        target_shares: 目标股数。
        delta_shares: 需要增减的股数。
        ref_price: 参考价格。
        reason: 下单原因。

    Returns:
        OrderIntent: 订单意图对象。
    """

    symbol: str
    side: str
    target_shares: int
    delta_shares: int
    ref_price: float
    reason: str

    def notional(self) -> float:
        """计算订单金额。

        Args:
            None

        Returns:
            float: 订单名义金额。
        """
        return abs(float(self.delta_shares) * float(self.ref_price))

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。

        Args:
            None

        Returns:
            Dict[str, Any]: 可序列化字典。
        """
        return asdict(self)


@dataclass
class FillRecord:
    """成交记录对象。

    Args:
        symbol: 证券代码。
        side: 买卖方向。
        shares: 成交股数。
        price: 成交价。
        gross_amount: 成交金额。
        fee: 手续费。
        net_cash_flow: 现金流。
        order_id: 委托编号。
        exec_id: 成交编号。

    Returns:
        FillRecord: 成交记录对象。
    """

    symbol: str
    side: str
    shares: int
    price: float
    gross_amount: float
    fee: float
    net_cash_flow: float
    order_id: str = ""
    exec_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。

        Args:
            None

        Returns:
            Dict[str, Any]: 可序列化字典。
        """
        return asdict(self)


@dataclass
class AccountState:
    """账户状态对象。

    Args:
        account_id: 账户编号。
        cash: 可用现金。
        nav_value: 账户净值。
        positions: 持仓列表。

    Returns:
        AccountState: 账户状态对象。
    """

    account_id: str
    cash: float
    nav_value: float | None = None
    positions: List[Position] = field(default_factory=list)

    def nav(self) -> float:
        """计算账户净值。

        Args:
            None

        Returns:
            float: 账户净值。
        """
        if self.nav_value is not None:
            return float(self.nav_value)
        return float(self.cash) + sum(pos.market_value() for pos in self.positions)

    def to_dict(self) -> Dict[str, Any]:
        """转为字典。

        Args:
            None

        Returns:
            Dict[str, Any]: 可序列化字典。
        """
        return {
            "account_id": self.account_id,
            "cash": self.cash,
            "nav": self.nav(),
            "positions": [pos.to_dict() for pos in self.positions],
        }
