# -*- coding: utf-8 -*-
"""???????????"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
from typing import List, Optional

import pandas as pd


DATE_CANDIDATES = ['date', 'trade_date', 'datetime']
CODE_CANDIDATES = ['ts_code', 'code', 'symbol']
INDUSTRY_CANDIDATES = ['industry', 'sw_level1', 'sector']

RESERVED_EXACT = {
    'year', 'month', 'day', 'pred_score', 'portfolio_weight', 'cash_weight', 'weight',
    'is_st', 'is_limit', 'is_suspended', 'is_tradable_basic', 'in_hs300', 'board_code',
    'industry_code', 'close_raw',  # close_raw 仅供回测价格地板用，不作训练特征(否则泄漏价格尺度)
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
    # 只取 data_root 顶层的聚合分片；不递归进 staging/ 和 _meta/。
    # staging/raw/YYYY/YYYYMMDD.parquet 是按天的原始中间产物（5000+ 文件），
    # 与顶层聚合分片是同一批数据的另一种切分，递归抓取会让训练表行数翻倍。
    _EXCLUDE_DIRS = {'staging', '_meta', '_cache', '_rebuild_tmp'}
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
        try:
            rel_parts = {part.lower() for part in path_item.relative_to(data_root).parts[:-1]}
        except ValueError:
            rel_parts = set()
        if rel_parts & _EXCLUDE_DIRS:
            continue
        out.append(path_item)
    return out


def _read_file(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == '.parquet':
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _files_signature(files: List[Path]) -> str:
    """按源文件的路径+大小+修改时间算指纹；任一源文件变了指纹就变，缓存自动失效。"""
    parts: List[str] = []
    for fp in sorted(files, key=lambda p: str(p)):
        try:
            st = fp.stat()
            parts.append(f'{fp.name}|{st.st_size}|{st.st_mtime_ns}')
        except OSError:
            parts.append(f'{fp.name}|na')
    raw = '\n'.join(parts).encode('utf-8', 'ignore')
    return hashlib.md5(raw).hexdigest()[:16]


def _load_concat_with_cache(files: List[Path], data_root: Path) -> pd.DataFrame:
    """读全部源文件并 concat；命中合并缓存时只读 1 个文件（P3 数据加载提速）。

    每个候选都是独立子进程、各自重读 34 个分片，是数据加载瓶颈。
    首次构建合并 parquet 落盘到 data_root/_cache，后续候选直接读这一个文件。
    源文件指纹变化时旧缓存自动忽略并清理。
    """
    if data_root.is_file() or len(files) <= 1:
        parts = [_read_file(fp) for fp in files]
        return pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    cache_dir = data_root / '_cache'
    sig = _files_signature(files)
    cache_path = cache_dir / f'combined_{sig}.parquet'
    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            try:
                cache_path.unlink()
            except OSError:
                pass

    parts = [_read_file(fp) for fp in files]
    df = pd.concat(parts, ignore_index=True)
    # 各分片的日期列存储格式不一（字符串/Timestamp 混存 → object 列），
    # pyarrow 无法直接落盘；写缓存前统一规范成 datetime（to_datetime 幂等，不影响下游）。
    for _dc in DATE_CANDIDATES:
        if _dc in df.columns and df[_dc].dtype == object:
            df[_dc] = pd.to_datetime(df[_dc], errors='coerce')
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 清理同目录下过期的合并缓存，避免无限堆积。
        for stale in cache_dir.glob('combined_*.parquet'):
            if stale.name != cache_path.name:
                try:
                    stale.unlink()
                except OSError:
                    pass
        tmp_path = cache_dir / f'.{cache_path.name}.tmp'
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(cache_path)
    except Exception:
        # 缓存写失败不影响主流程（如磁盘满/并发竞争），直接返回已读好的数据。
        pass
    return df


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

    df = _load_concat_with_cache(files, data_root)
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
