# -*- coding: utf-8 -*-
"""研究路线调度器。"""

from __future__ import annotations

from typing import Any, Dict, List


def allocate_route_budget(diagnosis: Dict[str, Any], total_candidates: int, min_each: int = 1) -> Dict[str, int]:
    """按诊断结果分配候选实验预算。

    Args:
        diagnosis: 诊断结果。
        total_candidates: 本轮总候选数。
        min_each: 每条路线最小候选数。

    Returns:
        路线预算。
    """
    route_weights = dict(diagnosis.get('route_weights', {}))
    if not route_weights:
        route_weights = {'feature': 1, 'model': 1, 'training': 1, 'portfolio': 1, 'risk': 1, 'data': 1, 'hybrid': 1}
    routes = list(route_weights.keys())
    total_weight = sum(max(float(v), 0.0) for v in route_weights.values()) or 1.0
    budget = {r: min_each for r in routes}
    remain = max(total_candidates - min_each * len(routes), 0)
    frac = {r: remain * float(route_weights[r]) / total_weight for r in routes}
    for r in routes:
        budget[r] += int(frac[r])
    assigned = sum(budget.values())
    leftovers = total_candidates - assigned
    ranked = sorted(routes, key=lambda x: frac[x] - int(frac[x]), reverse=True)
    idx = 0
    while leftovers > 0 and ranked:
        budget[ranked[idx % len(ranked)]] += 1
        leftovers -= 1
        idx += 1
    return budget


def route_hypotheses(diagnosis: Dict[str, Any]) -> Dict[str, List[str]]:
    """给每条研究路线提供假设。

    Args:
        diagnosis: 诊断结果。

    Returns:
        路线到假设列表的映射。
    """
    issues = set(diagnosis.get('issues', []))
    hypotheses = {
        'feature': [
            '当前特征空间过窄，需要扩充动量、波动、流动性、相对强弱与交互项。',
            '引入新特征组合可能比继续调模型参数更有效。',
        ],
        'model': [
            '当前模型家族过窄，需要引入不同归纳偏置的模型。',
            '不同模型家族在当前市场状态下可能出现互补。',
            '大样本路线应优先尝试 GPU 友好的树模型，而不是在 CPU 上硬跑随机森林。',
        ],
        'training': [
            '当前训练逻辑可能过拟合，需要更强正则与更近端加权。',
            '训练逻辑应从“全样本同权”转向“近端更高权重”。',
        ],
        'portfolio': [
            'Alpha 也许存在，但组合构建把它吃掉了。',
            '需要调整持仓数、行业约束与总仓位模板。',
        ],
        'risk': [
            '回撤主要来自风险预算而不是模型本身。',
            '需要重写弱市与回撤阶段的仓位控制。',
        ],
        'data': [
            '现有数据维度可能不够，需要侦察新的数据方向。',
            '引入更有解释力的新字段可能优于继续调旧特征。',
        ],
        'hybrid': [
            '需要跨路线联合试验，而不是单点修补。',
            '当前最优方向可能是“特征+模型”或“模型+训练”的联动变体。',
        ],
    }
    if 'weak_alpha' in issues:
        hypotheses['feature'].append('如果样本外 IC 持续偏弱，应优先扩大特征表达能力。')
    if 'overfit' in issues:
        hypotheses['training'].append('验证集与测试集落差过大，说明训练逻辑需要降复杂度。')
    if 'high_drawdown' in issues:
        hypotheses['risk'].append('最大回撤过高，风险预算路线必须提高优先级。')
    if 'gpu_gap' in issues:
        hypotheses['model'].append('当前 GPU 主路线直接固定为 xgboost_gpu，先把这条线跑透。')
    return hypotheses
