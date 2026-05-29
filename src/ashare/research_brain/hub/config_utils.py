# -*- coding: utf-8 -*-
"""配置读取与校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


REQUIRED_TOP_KEYS = [
    'project_name',
    'project_root',
    'train_table_dir',
    'hub_output_root',
    'execution',
    'strategy',
    'research_brain',
    'route_space',
]


def load_config(path_str: str) -> Dict[str, Any]:
    """读取 JSON 配置。

    Args:
        path_str: 配置文件路径。

    Returns:
        配置字典。
    """
    path = Path(path_str)
    text = path.read_text(encoding='utf-8-sig')
    return json.loads(text)


def ensure_required_keys(config: Dict[str, Any]) -> None:
    """检查必要字段。

    Args:
        config: 配置字典。

    Returns:
        None
    """
    missing: List[str] = [k for k in REQUIRED_TOP_KEYS if k not in config]
    if missing:
        raise KeyError(f'配置缺少必要字段: {missing}')
    if 'python_executable' not in config.get('execution', {}):
        raise KeyError('execution.python_executable 缺失')
    if 'label_col' not in config.get('strategy', {}):
        raise KeyError('strategy.label_col 缺失')
    if 'cycle_candidate_budget' not in config.get('research_brain', {}):
        raise KeyError('research_brain.cycle_candidate_budget 缺失')
