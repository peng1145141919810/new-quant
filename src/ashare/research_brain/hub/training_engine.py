# -*- coding: utf-8 -*-
"""训练与预测引擎。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from hub.dataset import DatasetBundle, infer_label_horizon, split_by_dates
from hub.io_utils import write_csv, write_json
from hub.metrics import build_overfit_diagnostics, summarize_prediction_frame
from hub.model_families import build_model


class ResourceBudgetSkip(RuntimeError):
    """资源预算触发的主动跳过。"""

    def __init__(self, message: str, meta: Optional[Dict[str, Any]] = None):
        """初始化异常。

        Args:
            message: 报错信息。
            meta: 资源元信息。

        Returns:
            None
        """
        super().__init__(message)
        self.meta = dict(meta or {})


DATE_CAP_HEAVY_FAMILIES = {'random_forest', 'extra_trees'}
GPU_FAMILIES = {'lightgbm_gpu', 'xgboost_gpu'}
# 树模型原生支持缺失值，应让 NaN 透传由模型自己分裂学习，而不是填 0（填 0 会把
# "缺失"伪装成一个极端数值、制造假信号）。线性模型（如 ridge_ranker）无法吃 NaN，
# 用【训练集每列中位数】回填（中位数来自 train，无未来泄漏）。
TREE_FAMILIES = {'lightgbm_gpu', 'lightgbm_auto', 'xgboost_gpu', 'hist_gbdt'}


def _env_flag(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _sample_prediction_frame(df: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    if max_rows <= 0 or len(df) <= max_rows:
        return df.copy()
    positions = np.linspace(0, len(df) - 1, num=max_rows, dtype=int)
    return df.iloc[positions].copy()


def _candidate_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _resolve_execution_lag_bars(candidate: Dict[str, Any]) -> int:
    return max(_candidate_int(candidate.get('execution_lag_bars', 1), 1), 0)


def _resolve_label_horizon(candidate: Dict[str, Any]) -> int:
    direct = _candidate_int(candidate.get('label_horizon', 0), 0)
    if direct > 0:
        return direct
    return infer_label_horizon(str(candidate.get('label_col', 'future_ret_5')), default=5)


def _normalize_policy_name(value: Any, default: str) -> str:
    text = str(value or '').strip().lower()
    return text or default


def _derive_execution_aligned_label(
    df: pd.DataFrame,
    *,
    code_col: str,
    date_col: str,
    base_label_col: str,
    label_horizon: int,
    execution_lag_bars: int,
) -> Tuple[pd.DataFrame, str, Dict[str, Any]]:
    if execution_lag_bars <= 0 or "close" not in df.columns:
        return df, base_label_col, {
            "base_label_col": base_label_col,
            "effective_label_col": base_label_col,
            "label_horizon": int(label_horizon),
            "execution_lag_bars": int(execution_lag_bars),
            "derived_execution_label": False,
        }
    out = df.sort_values([code_col, date_col]).copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    grouped = close.groupby(out[code_col])
    effective_label_col = f"{base_label_col}__entry_lag_{int(execution_lag_bars)}"
    out[effective_label_col] = grouped.shift(-(label_horizon + execution_lag_bars)) / grouped.shift(-execution_lag_bars) - 1.0
    non_null_ratio = float(pd.to_numeric(out[effective_label_col], errors="coerce").notna().mean()) if len(out.index) else 0.0
    return out, effective_label_col, {
        "base_label_col": base_label_col,
        "effective_label_col": effective_label_col,
        "label_horizon": int(label_horizon),
        "execution_lag_bars": int(execution_lag_bars),
        "derived_execution_label": True,
        "effective_label_non_null_ratio": round(non_null_ratio, 6),
    }


def _cross_section_rank(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.notna()
    if int(valid.sum()) <= 1:
        return pd.Series(0.0, index=series.index, dtype=float)
    ranked = numeric.rank(method="average", pct=True)
    return (ranked * 2.0 - 1.0).fillna(0.0)


def _cross_section_zscore(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    mean = numeric.mean()
    std = numeric.std(ddof=0)
    if not np.isfinite(std) or float(std) <= 1e-12:
        return pd.Series(0.0, index=series.index, dtype=float)
    return ((numeric - mean) / std).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _derive_alpha_training_label(
    df: pd.DataFrame,
    *,
    date_col: str,
    industry_col: Optional[str],
    realized_label_col: str,
    label_mode: str,
) -> Tuple[pd.DataFrame, str, Dict[str, Any]]:
    mode = _normalize_policy_name(label_mode, "raw_return")
    if mode in {"raw", "raw_return", "absolute_return"}:
        return df, realized_label_col, {
            "label_mode": "raw_return",
            "realized_label_col": realized_label_col,
            "training_label_col": realized_label_col,
            "training_label_derived": False,
        }

    out = df.copy()
    if mode in {"cross_section_rank", "cs_rank", "daily_rank"}:
        training_label_col = f"{realized_label_col}__cs_rank"
        out[training_label_col] = out.groupby(date_col, group_keys=False)[realized_label_col].apply(_cross_section_rank)
        effective_mode = "cross_section_rank"
    elif mode in {"cross_section_zscore", "cs_zscore", "daily_zscore"}:
        training_label_col = f"{realized_label_col}__cs_zscore"
        out[training_label_col] = out.groupby(date_col, group_keys=False)[realized_label_col].apply(_cross_section_zscore)
        effective_mode = "cross_section_zscore"
    elif mode in {"industry_neutral_rank", "industry_rank"} and industry_col and industry_col in out.columns:
        training_label_col = f"{realized_label_col}__industry_rank"
        out[training_label_col] = out.groupby([date_col, industry_col], group_keys=False)[realized_label_col].apply(_cross_section_rank)
        effective_mode = "industry_neutral_rank"
    else:
        training_label_col = f"{realized_label_col}__cs_rank"
        out[training_label_col] = out.groupby(date_col, group_keys=False)[realized_label_col].apply(_cross_section_rank)
        effective_mode = "cross_section_rank_fallback"

    non_null_ratio = float(pd.to_numeric(out[training_label_col], errors="coerce").notna().mean()) if len(out.index) else 0.0
    return out, training_label_col, {
        "label_mode": effective_mode,
        "requested_label_mode": mode,
        "realized_label_col": realized_label_col,
        "training_label_col": training_label_col,
        "training_label_derived": True,
        "training_label_non_null_ratio": round(non_null_ratio, 6),
    }


def _is_market_beta_feature(name: str) -> bool:
    text = str(name or "").strip().lower()
    if not text:
        return False
    exact = {
        "hs300_close",
        "hs300_open",
        "hs300_high",
        "hs300_low",
        "hs300_volume",
        "hs300_amount",
        "index_close",
        "index_open",
        "index_high",
        "index_low",
    }
    if text in exact:
        return True
    return text.startswith(("hs300_", "index_ret_", "market_ret_", "market_beta_", "benchmark_"))


def _apply_feature_beta_policy(selected: List[str], candidate: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    policy = _normalize_policy_name(candidate.get("feature_market_policy"), "allow")
    if policy in {"allow", "include", "none"}:
        return list(selected), {
            "feature_market_policy": policy,
            "market_beta_features_excluded": 0,
            "excluded_market_beta_feature_names": [],
        }
    if policy not in {"exclude_from_stock_ranker", "exclude", "stock_ranker_no_market_beta"}:
        return list(selected), {
            "feature_market_policy": f"{policy}_unknown_allow",
            "market_beta_features_excluded": 0,
            "excluded_market_beta_feature_names": [],
        }
    excluded = [c for c in selected if _is_market_beta_feature(c)]
    kept = [c for c in selected if c not in set(excluded)]
    if not kept:
        return list(selected), {
            "feature_market_policy": f"{policy}_fallback_allow_all",
            "market_beta_features_excluded": 0,
            "excluded_market_beta_feature_names": [],
        }
    return kept, {
        "feature_market_policy": "exclude_from_stock_ranker",
        "market_beta_features_excluded": int(len(excluded)),
        "excluded_market_beta_feature_names": excluded[:50],
    }


def _is_liquidity_feature(name: str) -> bool:
    """流动性特征：amount / 成交额 / 换手率 派生列。

    用户优先级 #4：把流动性当 hard filter（已经在 portfolio_recommendation
    _filter_executable_candidates 里），不要再作为 LightGBM 的 feature，
    避免模型偏向"成交活跃 → 涨"这种伪 alpha；同时也减少小盘股污染。

    保留：vol_* (波动率) — 波动率可能携带真实 alpha (低波异象)
    """
    text = str(name or "").strip().lower()
    if not text:
        return False
    exact = {
        "amount",
        "turnover_rate",
        "liquidity_amount_cny",
    }
    if text in exact:
        return True
    return text.startswith((
        "amount_",
        "turnover_mean_",
        "turnover_z_",
        "liquidity_amount_",
    ))


def _apply_feature_liquidity_policy(selected: List[str], candidate: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    policy = _normalize_policy_name(candidate.get("feature_liquidity_policy"), "allow")
    if policy in {"allow", "include", "none"}:
        return list(selected), {
            "feature_liquidity_policy": policy,
            "liquidity_features_excluded": 0,
            "excluded_liquidity_feature_names": [],
        }
    if policy not in {"exclude_from_stock_ranker", "exclude", "stock_ranker_no_liquidity"}:
        return list(selected), {
            "feature_liquidity_policy": f"{policy}_unknown_allow",
            "liquidity_features_excluded": 0,
            "excluded_liquidity_feature_names": [],
        }
    excluded = [c for c in selected if _is_liquidity_feature(c)]
    kept = [c for c in selected if c not in set(excluded)]
    if not kept:
        return list(selected), {
            "feature_liquidity_policy": f"{policy}_fallback_allow_all",
            "liquidity_features_excluded": 0,
            "excluded_liquidity_feature_names": [],
        }
    return kept, {
        "feature_liquidity_policy": "exclude_from_stock_ranker",
        "liquidity_features_excluded": int(len(excluded)),
        "excluded_liquidity_feature_names": excluded[:50],
    }


def _assert_codegen_valid(candidate: Dict[str, Any]) -> None:
    """Stop invalid generated modules from entering training."""
    lab = dict(candidate.get('lab', {}) or {})
    validations = dict(lab.get('validations', {}) or {})
    failed = {
        name: result
        for name, result in validations.items()
        if isinstance(result, dict) and not bool(result.get('ok', False))
    }
    if not failed:
        return
    meta = {
        'budget_action': 'skip_invalid_codegen',
        'requested_model_family': str(candidate.get('model_family', '')),
        'invalid_modules': list(failed.keys()),
        'validation_errors': {name: str(result.get('error', ''))[:2000] for name, result in failed.items()},
    }
    names = ', '.join(failed.keys())
    raise ResourceBudgetSkip(f'invalid generated modules: {names}', meta=meta)


def _clean_feature_frame(
    df: pd.DataFrame,
    cols: List[str],
    fill_values: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """整理特征矩阵。

    fill_values 为 None  → 保留 NaN（树模型路线，让模型自己学缺失分裂）。
    fill_values 为 Series → 按该值回填（线性模型路线，传训练集中位数，避免泄漏）。
    """
    # float32 而非 float64：训练表行数 ~1500 万 × 70+ 列，float64 的稠密块会吃 ~17GB。
    # GBDT/线性排序对 float32 精度完全够用，内存直接砍半。
    out = df[cols].replace([np.inf, -np.inf], np.nan).astype(np.float32)
    if fill_values is not None:
        out = out.fillna(fill_values).fillna(0.0)
    return out


def _project_runtime_frame(
    df: pd.DataFrame,
    code_col: str,
    label_col: str,
    extra_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    keep = [
        'date',
        code_col,
        'code',
        'ts_code',
        'board',
        'industry',
        'listed_days',
        'in_hs300',
        'is_st',
        'is_suspended',
        'is_limit',
        'is_tradable_basic',
        'close',
        'pct_chg',
        'amount',
        'amount_mean_20',
        'vol_20',
        'hs300_ret_20',
        label_col,
    ]
    if extra_cols:
        keep.extend(extra_cols)
    cols = [col for col in dict.fromkeys(keep) if col in df.columns]
    return df[cols].copy()


def _predict_scores(model: Any, X: pd.DataFrame, realized_family: str) -> np.ndarray:
    """统一预测入口，避免 XGBoost sklearn 包装器的 device mismatch 回退。"""
    if realized_family == 'xgboost_gpu' and model.__class__.__module__.startswith('xgboost'):
        try:
            import xgboost as xgb  # type: ignore

            booster = model.get_booster()
            dmatrix = xgb.DMatrix(X, feature_names=list(X.columns))
            return booster.predict(dmatrix)
        except Exception:
            pass
    return np.asarray(model.predict(X), dtype=float)


def _load_feature_transform(path: Optional[Path]):
    """加载候选特征变换函数。

    Args:
        path: 特征包路径。

    Returns:
        transform_features 函数或 None。
    """
    if path is None or not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location('feature_pack', path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return getattr(module, 'transform_features', None)
    except Exception:
        return None


def _load_training_override(path: Optional[Path]) -> Dict[str, Any]:
    """加载训练计划覆盖。

    Args:
        path: 覆盖模块路径。

    Returns:
        计划字典。
    """
    if path is None or not path.exists():
        return {}
    try:
        spec = importlib.util.spec_from_file_location('train_override', path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        if hasattr(module, 'override_training_plan'):
            return dict(module.override_training_plan({}) or {})
    except Exception:
        return {}
    return {}


def _select_features(df: pd.DataFrame, feature_cols: List[str], profile: str) -> List[str]:
    """按特征档案筛特征。

    Args:
        df: 数据表。
        feature_cols: 基础特征列。
        profile: 档案名。

    Returns:
        选中的特征列。
    """
    cols = list(feature_cols)
    if profile == 'baseline_plus':
        return cols
    if profile == 'momentum_cross_section':
        # 风险调整中长周期动量：A 股短周期(ret_1/ret_5)是强反转噪声，剔除；
        # 保留中长动量 + 行业中性动量(alpha_ret)，再带波动率让模型做风险调整压回撤。
        _reversal = {'ret_1', 'ret_5'}
        out = [
            c for c in cols
            if c not in _reversal
            and (
                c.startswith('ret_')
                or c.startswith('alpha_ret_')
                or c.startswith('vol_')
            )
        ]
        return out or cols
    if profile == 'vol_liq_quality':
        out = [c for c in cols if c.startswith(('vol_', 'amount_', 'turnover_')) or c in {'listed_days', 'total_mv', 'circ_mv'}]
        return out or cols
    if profile == 'defensive_residual':
        keep = []
        for c in cols:
            if c.startswith(('alpha_ret_', 'vol_', 'hs300_ret_')) or c in {'listed_days', 'amount_z_20'}:
                keep.append(c)
        return keep or cols
    if profile in {'interaction_sparse', 'generated_feature_pack'}:
        return cols
    return cols


def _apply_interaction_sparse(df: pd.DataFrame, cols: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    """加入稀疏交互项。

    Args:
        df: 数据表。
        cols: 原始特征列。

    Returns:
        (新数据表, 新特征列)
    """
    out = df.copy()
    pairs = [('ret_5', 'ret_20'), ('ret_20', 'vol_20'), ('alpha_ret_20_vs_hs300', 'vol_20')]
    extra_cols: List[str] = []
    for a, b in pairs:
        if a in out.columns and b in out.columns:
            name = f'inter_{a}__{b}'
            out[name] = out[a].astype(float) * out[b].astype(float)
            extra_cols.append(name)
    keep = list(dict.fromkeys(cols + extra_cols))
    return out, keep


def _apply_momentum_cross_section(
    df: pd.DataFrame,
    cols: List[str],
    date_col: str,
    industry_col: Optional[str] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """把动量特征做按日截面排名，得到真正的"相对强弱"截面动量。

    momentum_cross_section 原来只是挑了几个原始收益列，名字带 cross_section 却没有
    任何截面变换。这里在每个交易日内对全市场横向 rank 到 [0,1]，把绝对收益值转成
    "今天比同行强多少"——这才是经典的截面动量因子。按日分组、不跨日，无未来泄漏。
    原始列保留（模型可同时用绝对值和相对排名），新增 xs_* 排名列。

    Args:
        df: 数据表。
        cols: 选中的动量特征列。
        date_col: 日期列名（按它分组做截面）。
        industry_col: 行业列名（保留参数，当前用全市场截面更稳健）。

    Returns:
        (新数据表, 新特征列)
    """
    if date_col not in df.columns:
        return df, cols
    valid = [c for c in cols if c in df.columns]
    if not valid:
        return df, cols
    out = df.copy()
    numeric = out[valid].apply(pd.to_numeric, errors='coerce')
    ranked = numeric.groupby(out[date_col]).rank(pct=True)
    ranked.columns = [f'xs_{c}' for c in valid]
    out = pd.concat([out, ranked], axis=1)
    keep = list(dict.fromkeys(cols + list(ranked.columns)))
    return out, keep


def _recent_exponential_weights(dates: pd.Series) -> np.ndarray:
    """生成近端指数加权。

    Args:
        dates: 日期列。

    Returns:
        权重数组。
    """
    order = pd.Series(dates.rank(method='dense').astype(float))
    scaled = (order - order.min()) / max(order.max() - order.min(), 1.0)
    return np.exp(2.0 * scaled.to_numpy())


def _sample_train_rows(df: pd.DataFrame, max_rows: int, date_col: str, mode: str = 'recent_tail') -> pd.DataFrame:
    """对训练集做样本限流。

    Args:
        df: 训练集。
        max_rows: 最大样本数。
        date_col: 日期列。
        mode: 抽样模式。

    Returns:
        限流后的训练集。
    """
    if len(df) <= max_rows:
        return df
    if mode == 'recent_tail':
        return df.sort_values(date_col).tail(max_rows).copy()
    return df.sample(n=max_rows, random_state=42).copy()


def _estimate_compute_units(model_family: str, n_rows: int, n_features: int) -> float:
    """估算训练成本单位。

    Args:
        model_family: 模型家族。
        n_rows: 行数。
        n_features: 特征数。

    Returns:
        成本单位。
    """
    multiplier = {
        'lightgbm_auto': 1.0,
        'lightgbm_gpu': 0.75,
        'xgboost_gpu': 0.9,
        'ridge_ranker': 0.25,
        'elastic_net': 0.35,
        'hist_gbdt': 1.8,
        'extra_trees': 3.2,
        'random_forest': 5.5,
        'formula_blend': 0.1,
        'generated_family': 2.0,
    }.get(model_family, 1.5)
    return float(n_rows * max(n_features, 1) * multiplier / 1_000_000.0)


def _apply_budget_guard(
    train_df: pd.DataFrame,
    date_col: str,
    model_family: str,
    feature_count: int,
    constraints: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """应用资源预算约束。

    Args:
        train_df: 训练集。
        date_col: 日期列。
        model_family: 模型家族。
        feature_count: 特征数。
        constraints: 资源约束配置。

    Returns:
        (可能被限流后的训练集, 资源元信息)
    """
    family_rules = dict(constraints.get('family_rules', {})).get(model_family, {})
    max_rows = int(family_rules.get('max_train_rows', 0) or 0)
    skip_rows = int(family_rules.get('skip_if_train_rows_gt', 0) or 0)
    sample_mode = str(family_rules.get('sample_mode', 'recent_tail'))
    budget_action = 'full_train'

    meta = {
        'train_rows_before_sampling': int(len(train_df)),
        'train_rows_after_sampling': int(len(train_df)),
        'feature_count': int(feature_count),
        'budget_action': budget_action,
        'requested_model_family': model_family,
        'estimated_cost_units_before_sampling': _estimate_compute_units(model_family, len(train_df), feature_count),
        'estimated_cost_units_after_sampling': _estimate_compute_units(model_family, len(train_df), feature_count),
    }

    if skip_rows > 0 and len(train_df) > skip_rows and max_rows <= 0:
        meta['budget_action'] = 'skip_large_dataset'
        raise ResourceBudgetSkip(
            f'模型 {model_family} 在 train_rows={len(train_df)} 时超过 skip_if_train_rows_gt={skip_rows}，已主动跳过。',
            meta=meta,
        )

    if max_rows > 0 and len(train_df) > max_rows:
        train_df = _sample_train_rows(train_df, max_rows=max_rows, date_col=date_col, mode=sample_mode)
        meta['train_rows_after_sampling'] = int(len(train_df))
        meta['budget_action'] = f'sample_train::{sample_mode}'
        meta['estimated_cost_units_after_sampling'] = _estimate_compute_units(model_family, len(train_df), feature_count)

    return train_df, meta


def _fit_with_early_stopping(
    model: Any,
    realized_family: str,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_valid: pd.DataFrame,
    y_valid: pd.Series,
    base_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """用已切出的验证集对 LGBM/XGB 做早停，避免 420 棵树无脑跑满。

    Why: train_df/valid_df 早就切好了却没拿来早停，树跑满既慢又易过拟合。
    只对梯度提升树生效；ridge/extra_trees 等 sklearn 模型不支持 eval_set，走普通 fit。
    """
    kwargs = dict(base_kwargs)
    rounds = _env_int('RESEARCH_EARLY_STOP_ROUNDS', 60)
    name = model.__class__.__name__
    has_valid = X_valid is not None and len(X_valid) > 0
    meta: Dict[str, Any] = {'early_stopping_rounds': rounds if (rounds > 0 and has_valid) else 0}
    if rounds > 0 and has_valid and name.startswith('LGBM'):
        import lightgbm as lgb  # type: ignore
        kwargs['eval_set'] = [(X_valid, y_valid)]
        kwargs['eval_metric'] = 'l2'
        kwargs['callbacks'] = [lgb.early_stopping(rounds, verbose=False), lgb.log_evaluation(0)]
        meta['early_stopping_applied'] = 'lightgbm'
    elif rounds > 0 and has_valid and name.startswith('XGB'):
        try:
            model.set_params(early_stopping_rounds=rounds)
            kwargs['eval_set'] = [(X_valid, y_valid)]
            kwargs['verbose'] = False
            meta['early_stopping_applied'] = 'xgboost'
        except Exception:
            meta['early_stopping_applied'] = 'xgboost_setparams_failed'
    else:
        meta['early_stopping_applied'] = 'none'
    model.fit(X_train, y_train, **kwargs)
    best = getattr(model, 'best_iteration_', None) or getattr(model, 'best_iteration', None)
    if best is not None:
        meta['best_iteration'] = int(best)
    return meta


def train_and_predict(
    bundle: DatasetBundle,
    candidate: Dict[str, Any],
    run_dir: Path,
) -> Dict[str, Any]:
    """训练候选实验并输出摘要。

    Args:
        bundle: 数据包。
        candidate: 候选实验配置。
        run_dir: 运行目录。

    Returns:
        训练结果摘要。
    """
    _assert_codegen_valid(candidate)
    df = bundle.df.copy()
    feature_pack_path = Path(candidate['lab'].get('feature_pack_path', '')) if candidate.get('lab') else None
    train_override_path = Path(candidate['lab'].get('train_override_path', '')) if candidate.get('lab') else None
    generated_model_path = Path(candidate['lab'].get('generated_model_path', '')) if candidate.get('lab') else None

    feature_transform = _load_feature_transform(feature_pack_path)
    if feature_transform is not None:
        try:
            df = feature_transform(df)
        except Exception:
            pass

    feature_cols = [c for c in df.columns if c in bundle.feature_cols or c.startswith('feat_') or c.startswith('inter_')]
    selected = _select_features(df, feature_cols, candidate['feature_profile'])
    if candidate['feature_profile'] == 'interaction_sparse':
        df, selected = _apply_interaction_sparse(df, selected)
    elif candidate['feature_profile'] == 'momentum_cross_section':
        df, selected = _apply_momentum_cross_section(df, selected, bundle.date_col, bundle.industry_col)
    selected, feature_policy_meta = _apply_feature_beta_policy(selected, candidate)
    selected, liquidity_policy_meta = _apply_feature_liquidity_policy(selected, candidate)
    feature_policy_meta.update(liquidity_policy_meta)

    label_col = candidate['label_col']
    date_col = bundle.date_col
    code_col = bundle.code_col
    label_horizon = _resolve_label_horizon(candidate)
    execution_lag_bars = _resolve_execution_lag_bars(candidate)
    df, effective_label_col, time_alignment = _derive_execution_aligned_label(
        df,
        code_col=code_col,
        date_col=date_col,
        base_label_col=label_col,
        label_horizon=label_horizon,
        execution_lag_bars=execution_lag_bars,
    )
    realized_label_col = effective_label_col
    df, training_label_col, alpha_label_meta = _derive_alpha_training_label(
        df,
        date_col=date_col,
        industry_col=bundle.industry_col,
        realized_label_col=realized_label_col,
        label_mode=str(candidate.get("alpha_label_mode", "raw_return")),
    )
    split_embargo_days = max(label_horizon + execution_lag_bars, 0)
    train_df, valid_df, test_df = split_by_dates(df, date_col=date_col, embargo_days=split_embargo_days)
    time_alignment["split_embargo_days"] = int(split_embargo_days)

    # 剔除"没有真实未来收益"的样本（停牌、每段尾部 horizon 天等）。
    # 否则在 rank 标签模式下它们会被填成 0（中位数排名）硬塞进训练，污染信号、
    # 也让 test IC 把这些假样本算进去。注意只清训练/验证/测试三段，绝不动用于
    # latest 预测的 df —— 最新交易日的未来收益天然为空，但它不需要标签。
    def _drop_unlabeled(frame: pd.DataFrame) -> pd.DataFrame:
        # 同时要求"真实未来收益"和"训练标签"都非 NaN。
        # 行业中性排名标签（industry_neutral_rank）在行业缺失/分组过小的股票上算不出
        # 排名 → 标签 NaN；只清 realized_label_col 会把这些行漏掉，导致 ridge 等
        # 线性模型 fit 时报 "Input y contains NaN"。树模型恰好没踩到，但留着也是脏样本。
        mask = pd.Series(True, index=frame.index)
        for _col in (realized_label_col, training_label_col):
            if _col and _col in frame.columns:
                mask &= pd.to_numeric(frame[_col], errors="coerce").notna()
        return frame.loc[mask].copy()

    n_before = len(train_df) + len(valid_df) + len(test_df)
    train_df = _drop_unlabeled(train_df)
    valid_df = _drop_unlabeled(valid_df)
    test_df = _drop_unlabeled(test_df)
    time_alignment["rows_dropped_unlabeled"] = int(n_before - (len(train_df) + len(valid_df) + len(test_df)))

    train_plan = _load_training_override(train_override_path)
    _tl = str(candidate['training_logic'])
    if _tl in {'feature_select', 'generated_training'} or train_plan.get('feature_cap'):
        # 相对降维抗过拟合：generated_training 较激进(保留 65%)+近期加权，feature_select 温和(保留 75%)。
        # 旧逻辑默认 cap=60，对 30~38 列的生成特征包完全不起作用，使 training_logic 形同虚设。
        # 注：generated_training 原设 50% 太狠，实测欠拟合(得分 -2.0)，放宽到 65%。
        n_sel = len(selected)
        ratio = 0.65 if _tl == 'generated_training' else 0.75
        rel_cap = max(int(round(n_sel * ratio)), 5)
        cap = int(train_plan.get('feature_cap', rel_cap) or rel_cap)
        cap = max(min(cap, rel_cap, n_sel), 1)
        vars_ = train_df[selected].replace([np.inf, -np.inf], np.nan).astype(np.float32).fillna(0.0).var(axis=0).sort_values(ascending=False)
        selected = vars_.head(cap).index.tolist()

    constraints = dict(candidate.get('resource_constraints', {}))
    train_df, resource_meta = _apply_budget_guard(
        train_df=train_df,
        date_col=date_col,
        model_family=str(candidate['model_family']),
        feature_count=len(selected),
        constraints=constraints,
    )

    # 树模型让 NaN 透传；线性模型用训练集中位数回填（无未来泄漏）。
    is_tree_model = str(candidate['model_family']) in TREE_FAMILIES
    if is_tree_model:
        fill_values: Optional[pd.Series] = None
    else:
        fill_values = (
            train_df[selected].replace([np.inf, -np.inf], np.nan).astype(float).median()
        )

    X_train = _clean_feature_frame(train_df, selected, fill_values=fill_values)
    y_train = train_df[training_label_col].astype(float)
    X_valid = _clean_feature_frame(valid_df, selected, fill_values=fill_values)
    y_valid = valid_df[training_label_col].astype(float)
    X_test = _clean_feature_frame(test_df, selected, fill_values=fill_values)
    y_test = test_df[training_label_col].astype(float)

    if candidate['training_logic'] in {'weighted_recent', 'regularized_recent', 'generated_training'} or train_plan.get('sample_weight_mode') == 'recent_exponential':
        sample_weight = _recent_exponential_weights(train_df[date_col])
    else:
        sample_weight = None

    model_family = str(candidate['model_family'])
    model_options = dict(candidate.get('model_options', {}))
    model, realized_family, model_meta = build_model(model_family, generated_model_path=generated_model_path, model_options=model_options)
    fit_kwargs = {}
    if sample_weight is not None:
        fit_kwargs['sample_weight'] = sample_weight

    try:
        es_meta = _fit_with_early_stopping(model, realized_family, X_train, y_train, X_valid, y_valid, fit_kwargs)
        model_meta.update(es_meta)
    except Exception as exc:
        # GPU 路线失败时，允许回退到 CPU 版本，避免整轮报废。
        allow_gpu_fallback = bool(constraints.get('allow_gpu_fallback', True))
        if model_family in GPU_FAMILIES and allow_gpu_fallback:
            fallback_family = 'lightgbm_auto' if model_family == 'lightgbm_gpu' else 'hist_gbdt'
            model, realized_family, model_meta = build_model(fallback_family, generated_model_path=generated_model_path, model_options={})
            model_meta['gpu_fallback_reason'] = str(exc)
            es_meta = _fit_with_early_stopping(model, realized_family, X_train, y_train, X_valid, y_valid, fit_kwargs)
            model_meta.update(es_meta)
        else:
            raise
    if hasattr(model, 'export_meta'):
        try:
            model_meta.update(dict(model.export_meta() or {}))
        except Exception:
            pass

    pred_valid = _predict_scores(model=model, X=X_valid, realized_family=realized_family)
    pred_test = _predict_scores(model=model, X=X_test, realized_family=realized_family)

    pred_valid_df = valid_df[[date_col, code_col]].copy()
    pred_valid_df['pred_score'] = pred_valid
    pred_valid_df[training_label_col] = y_valid.to_numpy()
    if realized_label_col != training_label_col and realized_label_col in valid_df.columns:
        pred_valid_df[realized_label_col] = valid_df[realized_label_col].to_numpy()

    extra_cols = [realized_label_col]
    if training_label_col != realized_label_col:
        extra_cols.append(training_label_col)
    pred_test_df = _project_runtime_frame(test_df, code_col=code_col, label_col=label_col, extra_cols=extra_cols)
    pred_test_df['pred_score'] = pred_test

    latest_date = df[date_col].max()
    latest_source_df = df.loc[df[date_col] == latest_date].copy()
    X_latest = _clean_feature_frame(latest_source_df, selected, fill_values=fill_values)
    latest_df = _project_runtime_frame(latest_source_df, code_col=code_col, label_col=label_col)
    latest_df['pred_score'] = _predict_scores(model=model, X=X_latest, realized_family=realized_family)

    valid_metrics = summarize_prediction_frame(pred_valid_df, date_col=date_col, pred_col='pred_score', label_col=training_label_col)
    test_metrics = summarize_prediction_frame(pred_test_df, date_col=date_col, pred_col='pred_score', label_col=training_label_col)
    realized_valid_metrics = summarize_prediction_frame(pred_valid_df, date_col=date_col, pred_col='pred_score', label_col=realized_label_col) if realized_label_col in pred_valid_df.columns else {}
    realized_test_metrics = summarize_prediction_frame(pred_test_df, date_col=date_col, pred_col='pred_score', label_col=realized_label_col) if realized_label_col in pred_test_df.columns else {}
    overfit_diagnostics = build_overfit_diagnostics(
        pred_valid_df,
        pred_test_df,
        date_col=date_col,
        pred_col='pred_score',
        label_col=training_label_col,
    )

    resource_meta.update(model_meta)
    resource_meta.update(feature_policy_meta)
    resource_meta.update({
        'effective_model_family': realized_family,
        'effective_label_col': training_label_col,
        'realized_return_label_col': realized_label_col,
        'selected_feature_count': int(len(selected)),
        'latest_date': str(latest_date),
        'label_horizon': int(label_horizon),
        'execution_lag_bars': int(execution_lag_bars),
    })

    train_summary = {
        'strategy_name': candidate['strategy_name'],
        'label_col': label_col,
        'effective_label_col': training_label_col,
        'training_label_col': training_label_col,
        'realized_return_label_col': realized_label_col,
        'alpha_label_meta': alpha_label_meta,
        'model_family': model_family,
        'effective_model_family': realized_family,
        'feature_profile': candidate['feature_profile'],
        'training_logic': candidate['training_logic'],
        'n_features': int(len(selected)),
        'feature_policy_meta': feature_policy_meta,
        'valid_metrics': valid_metrics,
        'test_metrics': test_metrics,
        'realized_valid_metrics': realized_valid_metrics,
        'realized_test_metrics': realized_test_metrics,
        'overfit_diagnostics': overfit_diagnostics,
        'time_alignment': time_alignment,
        'resource_meta': resource_meta,
    }

    write_json(run_dir / 'train_summary.json', train_summary)
    pred_test_path = ''
    if _env_flag('ASHARE_WRITE_FULL_PRED_TEST_CSV', default=False):
        pred_test_path = str(run_dir / 'pred_test.csv')
        write_csv(Path(pred_test_path), pred_test_df)
    else:
        sample_rows = _env_int('ASHARE_PRED_TEST_SAMPLE_ROWS', default=20000)
        if sample_rows > 0:
            pred_test_path = str(run_dir / 'pred_test.sample.csv')
            write_csv(Path(pred_test_path), _sample_prediction_frame(pred_test_df, max_rows=sample_rows))
    write_csv(run_dir / 'latest_scores.csv', latest_df.sort_values('pred_score', ascending=False))

    feature_importance_df = pd.DataFrame(columns=['feature', 'importance'])
    if hasattr(model, 'feature_importances_'):
        feature_importance_df = pd.DataFrame({'feature': selected, 'importance': list(model.feature_importances_)})
    elif hasattr(model, 'coef_'):
        coefs = np.ravel(model.coef_)
        feature_importance_df = pd.DataFrame({'feature': selected, 'importance': np.abs(coefs)})
    else:
        feature_importance_df = pd.DataFrame({'feature': selected, 'importance': [0.0] * len(selected)})
    feature_importance_df = feature_importance_df.sort_values('importance', ascending=False)
    write_csv(run_dir / 'feature_importance.csv', feature_importance_df)

    return {
        'train_summary_path': str(run_dir / 'train_summary.json'),
        'pred_test_path': pred_test_path,
        'pred_test_df': pred_test_df,
        'latest_scores_path': str(run_dir / 'latest_scores.csv'),
        'feature_importance_path': str(run_dir / 'feature_importance.csv'),
        'train_summary': train_summary,
        'resource_meta': resource_meta,
    }
