# -*- coding: utf-8 -*-
"""通用 IO。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def ensure_dir(path: Path) -> Path:
    """确保目录存在。

    Args:
        path: 目录路径。

    Returns:
        目录路径。
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Any) -> None:
    """写 JSON。

    Args:
        path: 文件路径。
        payload: 数据。

    Returns:
        None
    """
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV。

    Args:
        path: 文件路径。
        df: 数据表。

    Returns:
        None
    """
    ensure_dir(path.parent)
    df.to_csv(path, index=False, encoding='utf-8-sig')
