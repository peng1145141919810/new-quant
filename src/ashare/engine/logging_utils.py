# -*- coding: utf-8 -*-
"""日志工具。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .config_utils import ensure_dir


def log_line(config: Dict[str, Any], message: str) -> Path:
    """写入日志并同时打印。

    Args:
        config: 运行配置。
        message: 日志文本。

    Returns:
        Path: 日志文件路径。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line)
    root = ensure_dir(Path(str(config["paths"]["log_root"])))
    path = root / f"run_{datetime.now().strftime('%Y%m%d')}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path
