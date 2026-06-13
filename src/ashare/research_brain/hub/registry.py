# -*- coding: utf-8 -*-
"""实验注册表。"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from hub.io_utils import ensure_dir, write_csv


@contextmanager
def _registry_lock(path: Path, timeout: float = 60.0, stale: float = 120.0):
    """跨进程文件锁：并行候选子进程各自 append 同一 registry，必须串行化
    读-concat-写，否则后写覆盖先写 / 留下半截 CSV，损坏冠军选择与续跑依据。
    用 O_CREAT|O_EXCL 独占创建 .lock 实现，超时抢占陈旧锁(进程崩溃残留)。"""
    ensure_dir(path.parent)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            # 抢占陈旧锁：持有者多半已崩溃
            try:
                if time.time() - os.path.getmtime(lock_path) > stale:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"registry lock 超时: {lock_path}")
            time.sleep(0.2)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


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
    row = {c: record.get(c) for c in REGISTRY_COLUMNS}
    with _registry_lock(path):
        # 锁内重新读，确保看到其它子进程刚写入的行（读-改-写整体原子）。
        df = load_registry(path)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        ensure_dir(path.parent)
        write_csv(path, df)
    return df
