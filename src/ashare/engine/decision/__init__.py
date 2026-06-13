# -*- coding: utf-8 -*-
"""单遍决策层（另起炉灶版）。

替换旧的“仲裁塔”：研究产出 -> 本层单遍定权 -> portfolio_control 机械落地。
本层只输出目标权重（List[TargetPosition]），不碰手数/T+1/下单，那些原样复用。

核心原则：
1. 约束集中在 DecisionConstraints 一个对象，不再散落各处。
2. 多来源上限取 min（不连乘）。
3. micro 账户专属：集中 3-5 只、单名 <= 25%、低回撤。
4. regime 闸门只做一次：panic 只减不加，caution 缩规模。
"""

from .constraints import DecisionConstraints, load_decision_constraints
from .engine import DecisionResult, decide_target_weights

__all__ = [
    "DecisionConstraints",
    "load_decision_constraints",
    "DecisionResult",
    "decide_target_weights",
]
