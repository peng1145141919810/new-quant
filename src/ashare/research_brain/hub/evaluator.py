# -*- coding: utf-8 -*-
"""统一评分器。"""

from __future__ import annotations

from typing import Any, Dict, Optional


def compute_total_score(
    train_summary: Dict[str, Any],
    portfolio_summary: Dict[str, Any],
    rules: Dict[str, Any],
    resource_meta: Optional[Dict[str, Any]] = None,
    elapsed_seconds: Optional[float] = None,
) -> float:
    """计算综合得分。

    Args:
        train_summary: 训练摘要。
        portfolio_summary: 组合摘要。
        rules: 权重配置。
        resource_meta: 资源使用元信息。
        elapsed_seconds: 单实验总耗时。

    Returns:
        综合得分。
    """
    valid = train_summary.get('valid_metrics', {})
    test = train_summary.get('test_metrics', {})
    score = 0.0
    score += float(valid.get('daily_rank_ic_mean', 0.0)) * float(rules.get('w_valid_ic', 200.0))
    score += float(test.get('daily_rank_ic_mean', 0.0)) * float(rules.get('w_test_ic', 250.0))
    score += float(valid.get('spearman_corr', 0.0)) * float(rules.get('w_valid_spearman', 150.0))
    score += float(test.get('spearman_corr', 0.0)) * float(rules.get('w_test_spearman', 200.0))
    score += float(portfolio_summary.get('annualized_ret', 0.0)) * float(rules.get('w_ret', 20.0))
    score += float(portfolio_summary.get('sharpe', 0.0)) * float(rules.get('w_sharpe', 10.0))
    score -= abs(min(float(portfolio_summary.get('max_drawdown', 0.0)), 0.0)) * float(rules.get('w_drawdown', 80.0))

    meta = dict(resource_meta or train_summary.get('resource_meta', {}) or {})
    score -= float(meta.get('estimated_cost_units_after_sampling', 0.0)) * float(rules.get('w_compute_cost', 0.18))
    if elapsed_seconds is not None:
        score -= (float(elapsed_seconds) / 60.0) * float(rules.get('w_runtime_minute', 0.06))
    if str(meta.get('budget_action', '')).startswith('sample_train'):
        score -= float(rules.get('w_sampling_penalty', 1.5))
    if 'gpu_fallback_reason' in meta:
        score -= float(rules.get('w_gpu_fallback_penalty', 2.0))
    if meta.get('budget_action') == 'skip_large_dataset':
        score -= float(rules.get('w_skip_penalty', 8.0))
    overfit = dict(train_summary.get('overfit_diagnostics', {}) or {})
    score -= float(overfit.get('risk_score', 0.0) or 0.0) * float(rules.get('w_overfit_risk', 12.0))
    if str(overfit.get('risk_level', '') or '') == 'high':
        score -= float(rules.get('w_overfit_high_penalty', 6.0))
    elif str(overfit.get('risk_level', '') or '') == 'medium':
        score -= float(rules.get('w_overfit_medium_penalty', 2.5))
    return float(score)
