# -*- coding: utf-8 -*-
"""研究诊断器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from hub.io_utils import write_json
from hub.registry import load_registry


DEFAULT_DIAGNOSIS = {
    'issues': ['cold_start'],
    'route_weights': {'feature': 2, 'model': 2, 'training': 2, 'portfolio': 1, 'risk': 1, 'data': 1, 'hybrid': 1},
    'summary': {},
}


def diagnose_research_state(registry_path: Path, output_path: Path) -> Dict[str, Any]:
    """诊断研究状态。

    Args:
        registry_path: 注册表路径。
        output_path: 输出 JSON 路径。

    Returns:
        诊断字典。
    """
    df = load_registry(registry_path)
    if df.empty:
        write_json(output_path, DEFAULT_DIAGNOSIS)
        return DEFAULT_DIAGNOSIS

    recent = df.tail(min(20, len(df))).copy()
    issues: List[str] = []
    summary: Dict[str, Any] = {}

    valid_ic = pd.to_numeric(recent['valid_ic'], errors='coerce').fillna(0.0)
    test_ic = pd.to_numeric(recent['test_ic'], errors='coerce').fillna(0.0)
    sharpe = pd.to_numeric(recent['sharpe'], errors='coerce').fillna(0.0)
    mdd = pd.to_numeric(recent['max_drawdown'], errors='coerce').fillna(0.0)
    total_score = pd.to_numeric(recent['total_score'], errors='coerce').fillna(0.0)

    summary['recent_valid_ic_mean'] = float(valid_ic.mean())
    summary['recent_test_ic_mean'] = float(test_ic.mean())
    summary['recent_sharpe_mean'] = float(sharpe.mean())
    summary['recent_drawdown_mean'] = float(mdd.mean())
    summary['recent_total_score_mean'] = float(total_score.mean())

    if float(test_ic.mean()) < 0.02:
        issues.append('weak_alpha')
    if float(valid_ic.mean()) - float(test_ic.mean()) > 0.03:
        issues.append('overfit')
    if abs(float(mdd.min())) > 0.25:
        issues.append('high_drawdown')
    if float(sharpe.mean()) < 0.6:
        issues.append('weak_risk_adjusted_return')
    if float(total_score.std(ddof=0)) > 25.0:
        issues.append('unstable_research')
    if recent['model_family'].nunique(dropna=True) <= 1:
        issues.append('narrow_model_family')
    if recent['feature_profile'].nunique(dropna=True) <= 1:
        issues.append('narrow_feature_space')
    gpu_families = {'lightgbm_gpu', 'xgboost_gpu'}
    if not set(recent['model_family'].dropna().astype(str)).intersection(gpu_families):
        issues.append('gpu_gap')

    route_weights = {'feature': 1, 'model': 1, 'training': 1, 'portfolio': 1, 'risk': 1, 'data': 1, 'hybrid': 1}
    for issue in issues:
        if issue in {'weak_alpha', 'narrow_feature_space'}:
            route_weights['feature'] += 2
            route_weights['data'] += 1
        if issue in {'overfit', 'narrow_model_family', 'gpu_gap'}:
            route_weights['model'] += 2
            route_weights['training'] += 2
        if issue in {'high_drawdown', 'weak_risk_adjusted_return'}:
            route_weights['portfolio'] += 2
            route_weights['risk'] += 2
        if issue == 'unstable_research':
            route_weights['hybrid'] += 2

    payload = {'issues': issues or ['no_critical_issue'], 'route_weights': route_weights, 'summary': summary}
    write_json(output_path, payload)
    return payload
