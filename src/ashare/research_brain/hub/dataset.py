# -*- coding: utf-8 -*-
"""???????????"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import List, Optional

import pandas as pd


DATE_CANDIDATES = ['date', 'trade_date', 'datetime']
CODE_CANDIDATES = ['ts_code', 'code', 'symbol']
INDUSTRY_CANDIDATES = ['industry', 'sw_level1', 'sector']

RESERVED_EXACT = {
    'year', 'month', 'day', 'pred_score', 'portfolio_weight', 'cash_weight', 'weight',
    'is_st', 'is_limit', 'is_suspended', 'is_tradable_basic', 'in_hs300', 'board_code',
    'industry_code'
}


@dataclass
class DatasetBundle:
    """??????"""

    df: pd.DataFrame
    date_col: str
    code_col: str
    industry_col: Optional[str]
    feature_cols: List[str]
    label_cols: List[str]


class DatasetError(Exception):
    """?????"""


def _detect_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _iter_data_files(data_root: Path) -> List[Path]:
    if data_root.is_file():
        return [data_root]
    patterns = ['*.parquet', '*.csv']
    files: List[Path] = []
    for p in patterns:
        files.extend(sorted(data_root.rglob(p)))
    out: List[Path] = []
    for path_item in files:
        name = path_item.name.lower()
        if name.startswith('build_'):
            continue
        if name.endswith('_manifest.csv') or name.endswith('_summary.csv'):
            continue
        out.append(path_item)
    return out


def _read_file(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == '.parquet':
        return pd.read_parquet(path)
    return pd.read_csv(path)


def load_training_table(
    data_root: Path,
    label_col: str,
    max_files: Optional[int] = None,
    sample_rows: Optional[int] = None,
) -> DatasetBundle:
    files = _iter_data_files(data_root)
    if not files:
        raise DatasetError(f'????????: {data_root}')
    if max_files is not None and max_files > 0:
        files = files[:max_files]

    parts: List[pd.DataFrame] = []
    for fp in files:
        parts.append(_read_file(fp))
    df = pd.concat(parts, ignore_index=True)
    if sample_rows is not None and sample_rows > 0:
        df = df.head(sample_rows).copy()

    date_col = _detect_col(df, DATE_CANDIDATES)
    code_col = _detect_col(df, CODE_CANDIDATES)
    industry_col = _detect_col(df, INDUSTRY_CANDIDATES)
    if date_col is None:
        raise DatasetError('????????????? date ? trade_date')
    if code_col is None:
        raise DatasetError('??????????????? ts_code ? code')
    if label_col not in df.columns:
        raise DatasetError(f'??????: {label_col}')

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values([date_col, code_col]).reset_index(drop=True)

    label_cols = [c for c in df.columns if c.startswith('future_ret_')]
    numeric_cols = df.select_dtypes(include=['number', 'bool']).columns.tolist()
    feature_cols: List[str] = []
    for c in numeric_cols:
        if c in RESERVED_EXACT:
            continue
        if c in label_cols:
            continue
        if c == label_col:
            continue
        feature_cols.append(c)

    if not feature_cols:
        raise DatasetError('??????????????')

    return DatasetBundle(
        df=df,
        date_col=date_col,
        code_col=code_col,
        industry_col=industry_col,
        feature_cols=feature_cols,
        label_cols=label_cols,
    )


def infer_label_horizon(label_col: str, default: int = 5) -> int:
    """Infer label horizon from names like future_ret_5."""
    match = re.search(r'(\d+)', str(label_col or ''))
    if not match:
        return int(default)
    try:
        return max(int(match.group(1)), 1)
    except Exception:
        return int(default)


def split_by_dates(
    df: pd.DataFrame,
    date_col: str,
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
    embargo_days: int = 0,
):
    dates = sorted(pd.Series(df[date_col].dropna().unique()).tolist())
    embargo = max(int(embargo_days or 0), 0)
    usable_n = len(dates) - 2 * embargo
    if usable_n < 10:
        raise DatasetError('?????????????/??/??')

    train_len = max(1, int(usable_n * train_ratio))
    valid_len = max(1, int(usable_n * valid_ratio))
    test_len = max(1, usable_n - train_len - valid_len)
    if train_len + valid_len + test_len > usable_n:
        test_len = max(1, usable_n - train_len - valid_len)
    if train_len + valid_len + test_len > usable_n:
        valid_len = max(1, usable_n - train_len - test_len)

    train_start = 0
    train_end = train_start + train_len
    valid_start = train_end + embargo
    valid_end = valid_start + valid_len
    test_start = valid_end + embargo
    test_end = test_start + test_len

    train_dates = set(dates[train_start:train_end])
    valid_dates = set(dates[valid_start:valid_end])
    test_dates = set(dates[test_start:test_end])
    if not train_dates or not valid_dates or not test_dates:
        raise DatasetError('?????????? embargo ??????')

    train_df = df.loc[df[date_col].isin(train_dates)].copy()
    valid_df = df.loc[df[date_col].isin(valid_dates)].copy()
    test_df = df.loc[df[date_col].isin(test_dates)].copy()
    return train_df, valid_df, test_df
