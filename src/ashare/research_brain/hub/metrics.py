# -*- coding: utf-8 -*-
"""评估指标。"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


def safe_corr(a: pd.Series, b: pd.Series, method: str = 'pearson') -> float:
    """安全相关系数。

    Args:
        a: 序列一。
        b: 序列二。
        method: 相关方法。

    Returns:
        float

    Notes:
        当其中一列方差为 0（例如截面 rank label 的某天 pred 完全相同），
        pandas.Series.corr 会返回 NaN。`x or 0.0` 在 Python 里对 NaN 不起作用
        （NaN 是 truthy），所以必须显式判 NaN。
    """
    df = pd.concat([a, b], axis=1).dropna()
    if len(df) < 3:
        return 0.0
    val = df.iloc[:, 0].corr(df.iloc[:, 1], method=method)
    if val is None or not np.isfinite(val):
        return 0.0
    return float(val)


def daily_rank_ic_mean(df: pd.DataFrame, date_col: str, pred_col: str, label_col: str) -> float:
    """逐日 rank IC 均值。

    Args:
        df: 数据表。
        date_col: 日期列。
        pred_col: 预测列。
        label_col: 标签列。

    Returns:
        float
    """
    if date_col not in df.columns or pred_col not in df.columns or label_col not in df.columns:
        return 0.0
    vals: List[float] = []
    for _, g in df.groupby(date_col):
        if len(g) < 5:
            continue
        ic = safe_corr(g[pred_col], g[label_col], method='spearman')
        # safe_corr 已保证不会返回 NaN/inf，但保险起见再过一遍
        if ic is not None and np.isfinite(ic):
            vals.append(float(ic))
    return float(np.mean(vals)) if vals else 0.0


def daily_rank_ic_series(df: pd.DataFrame, date_col: str, pred_col: str, label_col: str) -> pd.Series:
    """Per-date rank-IC series used for stability and overfit diagnostics."""
    if date_col not in df.columns or pred_col not in df.columns or label_col not in df.columns:
        return pd.Series(dtype="float64")
    rows: List[Dict[str, float]] = []
    for dt, g in df.groupby(date_col):
        if len(g) < 5:
            continue
        rows.append({"date": pd.to_datetime(dt), "rank_ic": safe_corr(g[pred_col], g[label_col], method="spearman")})
    if not rows:
        return pd.Series(dtype="float64")
    series = pd.DataFrame(rows).sort_values("date").set_index("date")["rank_ic"]
    return series.astype(float)


def build_overfit_diagnostics(
    valid_df: pd.DataFrame,
    test_df: pd.DataFrame,
    *,
    date_col: str,
    pred_col: str,
    label_col: str,
) -> Dict[str, float | str | List[str]]:
    """Build a compact overfit diagnostic report from valid/test predictions."""
    valid_ic = daily_rank_ic_mean(valid_df, date_col=date_col, pred_col=pred_col, label_col=label_col)
    test_ic = daily_rank_ic_mean(test_df, date_col=date_col, pred_col=pred_col, label_col=label_col)
    valid_s = safe_corr(valid_df[pred_col], valid_df[label_col], method="spearman")
    test_s = safe_corr(test_df[pred_col], test_df[label_col], method="spearman")

    valid_series = daily_rank_ic_series(valid_df, date_col=date_col, pred_col=pred_col, label_col=label_col)
    test_series = daily_rank_ic_series(test_df, date_col=date_col, pred_col=pred_col, label_col=label_col)
    test_negative_share = float((test_series < 0).mean()) if not test_series.empty else 1.0

    window_consistency = 0.0
    if len(test_series) >= 3:
        chunks = np.array_split(test_series.to_numpy(dtype=float), 3)
        window_means = [float(np.mean(chunk)) for chunk in chunks if len(chunk) > 0]
        if window_means:
            positive_windows = sum(1 for item in window_means if item > 0)
            window_consistency = positive_windows / max(len(window_means), 1)

    ic_gap = float(valid_ic - test_ic)
    spearman_gap = float(valid_s - test_s)
    test_vs_valid_ic_ratio = float(test_ic / max(abs(valid_ic), 1e-9)) if abs(valid_ic) > 1e-9 else 0.0

    flags: List[str] = []
    risk_score = 0.0
    if valid_ic > 0 and test_ic < 0:
        risk_score += 0.45
        flags.append("ic_sign_flip")
    if valid_s > 0 and test_s < 0:
        risk_score += 0.25
        flags.append("spearman_sign_flip")
    if ic_gap > 0.03:
        risk_score += min(ic_gap / 0.10, 0.20)
        flags.append("valid_test_ic_gap")
    if spearman_gap > 0.03:
        risk_score += min(spearman_gap / 0.10, 0.15)
        flags.append("valid_test_spearman_gap")
    if test_vs_valid_ic_ratio < 0.55:
        risk_score += 0.20
        flags.append("weak_test_ic_ratio")
    if test_negative_share > 0.55:
        risk_score += 0.15
        flags.append("test_rank_ic_negative_majority")
    if window_consistency and window_consistency < 0.34:
        risk_score += 0.15
        flags.append("test_window_instability")
    risk_score = float(min(risk_score, 1.0))

    risk_level = "low"
    if risk_score >= 0.65:
        risk_level = "high"
    elif risk_score >= 0.35:
        risk_level = "medium"

    return {
        "risk_score": round(risk_score, 6),
        "risk_level": risk_level,
        "flags": flags,
        "valid_daily_rank_ic_mean": round(float(valid_ic), 6),
        "test_daily_rank_ic_mean": round(float(test_ic), 6),
        "valid_test_ic_gap": round(ic_gap, 6),
        "valid_test_spearman_gap": round(spearman_gap, 6),
        "test_vs_valid_ic_ratio": round(test_vs_valid_ic_ratio, 6),
        "test_rank_ic_negative_share": round(test_negative_share, 6),
        "test_window_consistency": round(float(window_consistency), 6),
        "valid_rank_ic_days": int(len(valid_series)),
        "test_rank_ic_days": int(len(test_series)),
    }


def summarize_prediction_frame(df: pd.DataFrame, date_col: str, pred_col: str, label_col: str) -> Dict[str, float]:
    """汇总预测指标。

    Args:
        df: 数据表。
        date_col: 日期列。
        pred_col: 预测列。
        label_col: 标签列。

    Returns:
        指标字典。
    """
    return {
        'pearson_corr': safe_corr(df[pred_col], df[label_col], method='pearson'),
        'spearman_corr': safe_corr(df[pred_col], df[label_col], method='spearman'),
        'daily_rank_ic_mean': daily_rank_ic_mean(df, date_col=date_col, pred_col=pred_col, label_col=label_col),
    }


def annualized_from_period_returns(period_returns: Iterable[float], periods_per_year: int) -> float:
    """周期收益转年化。

    Args:
        period_returns: 周期收益序列。
        periods_per_year: 年内周期数。

    Returns:
        float
    """
    vals = np.asarray(list(period_returns), dtype=float)
    if vals.size == 0:
        return 0.0
    nav = np.cumprod(1.0 + vals)
    years = max(vals.size / float(max(periods_per_year, 1)), 1e-9)
    return float(nav[-1] ** (1.0 / years) - 1.0)


def sharpe_from_period_returns(period_returns: Iterable[float], periods_per_year: int) -> float:
    """周期收益转 Sharpe。

    Args:
        period_returns: 周期收益序列。
        periods_per_year: 年内周期数。

    Returns:
        float
    """
    vals = np.asarray(list(period_returns), dtype=float)
    if vals.size < 2:
        return 0.0
    mu = float(np.mean(vals))
    sd = float(np.std(vals, ddof=1))
    if sd <= 1e-12:
        return 0.0
    return float(mu / sd * np.sqrt(periods_per_year))


def max_drawdown_from_nav(nav: Iterable[float]) -> float:
    """净值最大回撤。

    Args:
        nav: 净值序列。

    Returns:
        float
    """
    arr = np.asarray(list(nav), dtype=float)
    if arr.size == 0:
        return 0.0
    peak = np.maximum.accumulate(arr)
    dd = arr / np.where(peak == 0, 1.0, peak) - 1.0
    return float(np.min(dd))
