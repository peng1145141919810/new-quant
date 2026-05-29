# -*- coding: utf-8 -*-
"""
掘金仿真最小测试单脚本。

Args:
    None

Returns:
    None
"""

import json
import time
from pathlib import Path

import gmtrade.api as gm


def load_config() -> dict:
    """
    读取本地配置文件。

    Args:
        None

    Returns:
        dict: 配置字典。
    """
    config_path = Path(__file__).parent / "gmtrade_local_config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def pick_attr(candidates):
    """
    从 gmtrade.api 中按顺序查找可用常量。

    Args:
        candidates (list[str]): 候选常量名列表。

    Returns:
        Any: 匹配到的常量值。

    Raises:
        AttributeError: 全部候选名都不存在。
    """
    for name in candidates:
        if hasattr(gm, name):
            return getattr(gm, name)
    raise AttributeError(f"未找到任何可用常量: {candidates}")


def main() -> None:
    """
    登录仿真账户并发送一笔最小限价测试单。

    Args:
        None

    Returns:
        None
    """
    cfg = load_config()
    probe = cfg["probe_order"]

    gm.set_token(cfg["token"])
    gm.set_endpoint(cfg.get("endpoint", "api.myquant.cn:9000"))

    acc = gm.account(
        account_id=cfg["account_id"],
        account_alias=cfg.get("account_alias", "")
    )
    gm.login(acc)

    print("开始发送测试单...")

    side = pick_attr(["OrderSide_Buy", "OrderSide_BuyOpen"])
    order_type = pick_attr(["OrderType_Limit", "OrderType_LimitOrder"])
    position_effect = getattr(gm, "PositionEffect_Open", None)
    price = float(probe["price"])

    kwargs = {
        "symbol": probe["symbol"],
        "volume": int(probe["volume"]),
        "side": side,
        "order_type": order_type,
        "price": price,
        "account": acc,
    }
    if position_effect is not None:
        kwargs["position_effect"] = position_effect

    order = gm.order_volume(**kwargs)
    print("委托返回：")
    print(order)

    print("\n等待 3 秒后查询未结委托与成交回报...")
    time.sleep(3)

    if hasattr(gm, "get_unfinished_orders"):
        print("\n=== 未结委托 ===")
        print(gm.get_unfinished_orders())

    if hasattr(gm, "get_execution_reports"):
        print("\n=== 成交回报 ===")
        print(gm.get_execution_reports())

    print("\n=== 最新资金 ===")
    print(gm.get_cash())

    if hasattr(gm, "get_positions"):
        print("\n=== 最新持仓 ===")
        print(gm.get_positions())
    elif hasattr(gm, "get_position"):
        print("\n=== 最新持仓 ===")
        print(gm.get_position())


if __name__ == "__main__":
    main()
