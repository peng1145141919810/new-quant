# -*- coding: utf-8 -*-
"""日志工具。"""

from __future__ import annotations

import logging
from pathlib import Path


_LOGGERS = {}


def setup_logger(log_dir: Path, name: str) -> logging.Logger:
    """创建日志器。

    Args:
        log_dir: 日志目录。
        name: 日志名称。

    Returns:
        logging.Logger
    """
    if name in _LOGGERS:
        return _LOGGERS[name]

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        fh = logging.FileHandler(log_dir / f'{name}.log', encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    _LOGGERS[name] = logger
    return logger
