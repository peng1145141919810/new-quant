# -*- coding: utf-8 -*-
"""
掘金仿真连接冒烟测试。

Args:
    None

Returns:
    None
"""

import json
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


def resolve_positions_func():
    """
    兼容不同 gmtrade 版本，寻找持仓查询函数。

    Args:
        None

    Returns:
        callable: 持仓查询函数。
    """
    if hasattr(gm, "get_positions"):
        return gm.get_positions
    if hasattr(gm, "get_position"):
        return gm.get_position
    raise AttributeError("当前 gmtrade.api 中既没有 get_positions，也没有 get_position")


def main() -> None:
    """
    执行登录、查询资金、查询持仓的冒烟测试。

    Args:
        None

    Returns:
        None
    """
    cfg = load_config()

    print("gmtrade.api 文件位置：", gm.__file__)
    print("gmtrade.api 中与 position/cash 相关的方法：")
    print([name for name in dir(gm) if ("position" in name.lower() or "cash" in name.lower())])

    gm.set_token(cfg["token"])
    gm.set_endpoint(cfg.get("endpoint", "api.myquant.cn:9000"))

    acc = gm.account(
        account_id=cfg["account_id"],
        account_alias=cfg.get("account_alias", "")
    )

    print("开始登录账户...")
    gm.login(acc)
    print("登录调用已完成。")

    print("\n=== 查询资金 ===")
    cash = gm.get_cash()
    print(cash)

    print("\n=== 查询持仓 ===")
    positions_func = resolve_positions_func()
    positions = positions_func()
    print(positions)


if __name__ == "__main__":
    main()
