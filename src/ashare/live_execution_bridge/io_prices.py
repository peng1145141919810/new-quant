from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .utils import normalize_symbol


PRICE_COLUMNS = ["price", "close", "last_price", "last", "adj_close", "open"]
SYMBOL_COLUMNS = ["symbol", "ts_code", "code", "stock_code", "ticker"]


def load_price_map(price_snapshot_path: str | Path) -> Dict[str, float]:
    """读取价格快照。

    Args:
        price_snapshot_path: 价格快照 CSV 路径。

    Returns:
        Dict[str, float]: 证券代码到价格的映射。
    """
    path = Path(price_snapshot_path)
    if not path.exists():
        raise FileNotFoundError(f"价格快照不存在: {path}")

    frame = pd.read_csv(path)
    lower_map = {str(col).strip().lower(): col for col in frame.columns}

    symbol_col = None
    for name in SYMBOL_COLUMNS:
        if name in lower_map:
            symbol_col = lower_map[name]
            break
    if symbol_col is None:
        raise ValueError(f"价格文件缺少证券代码列: {list(frame.columns)}")

    price_col = None
    for name in PRICE_COLUMNS:
        if name in lower_map:
            price_col = lower_map[name]
            break
    if price_col is None:
        raise ValueError(f"价格文件缺少价格列: {list(frame.columns)}")

    frame = frame.copy()
    frame["__symbol"] = frame[symbol_col].map(normalize_symbol)
    frame["__price"] = pd.to_numeric(frame[price_col], errors="coerce")
    frame = frame.dropna(subset=["__symbol", "__price"])
    frame = frame[frame["__price"] > 0]

    return {str(row["__symbol"]): float(row["__price"]) for _, row in frame.iterrows()}
