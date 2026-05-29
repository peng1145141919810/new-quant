# -*- coding: utf-8 -*-
"""实验注册表。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from hub.io_utils import ensure_dir, write_csv


REGISTRY_COLUMNS = [
    'run_id', 'cycle_id', 'cycle_index', 'strategy_family_name', 'strategy_name', 'strategy_key',
    'spec_hash', 'parent_strategy_key', 'research_route', 'hypothesis', 'feature_profile',
    'model_family', 'training_logic', 'label_col', 'label_horizon', 'top_k', 'config_path',
    'workspace_dir', 'train_summary_path', 'portfolio_summary_path', 'latest_portfolio_path', 'latest_scores_path',
    'pred_test_path', 'feature_importance_path', 'annualized_ret', 'sharpe', 'max_drawdown',
    'valid_ic', 'test_ic', 'valid_spearman', 'test_spearman', 'total_score', 'n_features',
    'effective_model_family', 'budget_action', 'estimated_cost_units', 'gpu_used', 'elapsed_seconds',
    'status', 'error_message', 'created_at'
]


def load_registry(path: Path) -> pd.DataFrame:
    """读取注册表。

    Args:
        path: 注册表路径。

    Returns:
        DataFrame
    """
    if not path.exists():
        return pd.DataFrame(columns=REGISTRY_COLUMNS)
    df = pd.read_csv(path)
    for c in REGISTRY_COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[REGISTRY_COLUMNS]


def append_record(path: Path, record: Dict[str, Any]) -> pd.DataFrame:
    """追加一条实验记录。

    Args:
        path: 注册表路径。
        record: 记录字典。

    Returns:
        更新后的 DataFrame。
    """
    df = load_registry(path)
    row = {c: record.get(c) for c in REGISTRY_COLUMNS}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    ensure_dir(path.parent)
    write_csv(path, df)
    return df
