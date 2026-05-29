# -*- coding: utf-8 -*-
"""V6 配置工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_config(config_path: Path) -> Dict[str, Any]:
    """读取配置文件。

    Args:
        config_path: 配置文件路径。

    Returns:
        Dict[str, Any]: 配置字典。
    """
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def ensure_dir(path: Path) -> Path:
    """确保目录存在。

    Args:
        path: 目录路径。

    Returns:
        Path: 已确保存在的目录路径。
    """
    path.mkdir(parents=True, exist_ok=True)
    return path
