# -*- coding: utf-8 -*-
"""统一约束对象。

旧系统把“单名上限”散在 >=4 个 config key、把集中度算 3 遍、把 reduce_only/
turnover 在 5-6 层各砍一刀并连乘。本模块把这些收敛成**一个** dataclass，
所有上限只在这里定义一次，决策引擎对多来源取 min 而非连乘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


def _f(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out


def _i(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class DecisionConstraints:
    """单遍决策的全部约束（micro 1-2万账户默认值）。

    Args:
        max_names: 同时最多持有的股票数。
        min_names: 满仓时期望的最小分散数（仅在候选充足时生效，不强制铺开）。
        single_name_cap: 单只股票最大占净值比例（硬上限，取 min 用）。
        total_exposure_cap: 总仓位上限（1.0 = 允许满仓，long-only）。
        min_name_weight: 单名最小权重，低于此则不开（避免碎仓）。
        caution_exposure_scale: caution regime 下总仓位缩放系数。
        panic_only_reduce: panic regime 下是否只允许减仓、不开新仓。
        cash_floor: 始终保留的最低现金比例。
    """

    max_names: int = 5
    min_names: int = 3
    single_name_cap: float = 0.25
    total_exposure_cap: float = 1.0
    min_name_weight: float = 0.08
    caution_exposure_scale: float = 0.6
    panic_only_reduce: bool = True
    cash_floor: float = 0.0

    def validated(self) -> "DecisionConstraints":
        """返回一个数值自洽的副本（修正越界/矛盾配置）。"""
        max_names = max(1, self.max_names)
        min_names = max(1, min(self.min_names, max_names))
        single = min(max(self.single_name_cap, 0.0), 1.0)
        # 单名上限必须能容纳 min_names 只满仓，否则放宽 min_names。
        if single * max_names < 1.0 and single > 0:
            feasible_min = max(1, int(1.0 / single + 0.999))  # 向上取整
            min_names = max(min_names, min(feasible_min, max_names))
        total = min(max(self.total_exposure_cap, 0.0), 1.0)
        return DecisionConstraints(
            max_names=max_names,
            min_names=min_names,
            single_name_cap=single,
            total_exposure_cap=total,
            min_name_weight=min(max(self.min_name_weight, 0.0), single),
            caution_exposure_scale=min(max(self.caution_exposure_scale, 0.0), 1.0),
            panic_only_reduce=bool(self.panic_only_reduce),
            cash_floor=min(max(self.cash_floor, 0.0), 1.0),
        )


def load_decision_constraints(config: Dict[str, Any]) -> DecisionConstraints:
    """从单一 config 段 `decision_engine` 读取约束（缺省即 micro 默认）。

    Args:
        config: 运行时配置字典。

    Returns:
        DecisionConstraints: 已校验的约束对象。
    """
    raw = dict((config or {}).get("decision_engine", {}) or {})
    return DecisionConstraints(
        max_names=_i(raw.get("max_names"), 5),
        min_names=_i(raw.get("min_names"), 3),
        single_name_cap=_f(raw.get("single_name_cap"), 0.25),
        total_exposure_cap=_f(raw.get("total_exposure_cap"), 1.0),
        min_name_weight=_f(raw.get("min_name_weight"), 0.08),
        caution_exposure_scale=_f(raw.get("caution_exposure_scale"), 0.6),
        panic_only_reduce=bool(raw.get("panic_only_reduce", True)),
        cash_floor=_f(raw.get("cash_floor"), 0.0),
    ).validated()
