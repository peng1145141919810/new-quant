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


def _clean_feature_frame(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    return df[cols].replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)


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
        out = [c for c in cols if c.startswith(('ret_', 'alpha_ret_', 'hs300_ret_'))]
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
    split_embargo_days = max(label_horizon + execution_lag_bars, 0)
    train_df, valid_df, test_df = split_by_dates(df, date_col=date_col, embargo_days=split_embargo_days)
    time_alignment["split_embargo_days"] = int(split_embargo_days)

    train_plan = _load_training_override(train_override_path)
    if candidate['training_logic'] == 'feature_select' or train_plan.get('feature_cap'):
        cap = int(train_plan.get('feature_cap', 60) or 60)
        vars_ = train_df[selected].replace([np.inf, -np.inf], np.nan).fillna(0.0).var(axis=0).sort_values(ascending=False)
        selected = vars_.head(min(cap, len(vars_))).index.tolist()

    constraints = dict(candidate.get('resource_constraints', {}))
    train_df, resource_meta = _apply_budget_guard(
        train_df=train_df,
        date_col=date_col,
        model_family=str(candidate['model_family']),
        feature_count=len(selected),
        constraints=constraints,
    )

    X_train = _clean_feature_frame(train_df, selected)
    y_train = train_df[effective_label_col].astype(float)
    X_valid = _clean_feature_frame(valid_df, selected)
    y_valid = valid_df[effective_label_col].astype(float)
    X_test = _clean_feature_frame(test_df, selected)
    y_test = test_df[effective_label_col].astype(float)

    if candidate['training_logic'] in {'weighted_recent', 'regularized_recent'} or train_plan.get('sample_weight_mode') == 'recent_exponential':
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
        model.fit(X_train, y_train, **fit_kwargs)
    except Exception as exc:
        # GPU 路线失败时，允许回退到 CPU 版本，避免整轮报废。
        allow_gpu_fallback = bool(constraints.get('allow_gpu_fallback', True))
        if model_family in GPU_FAMILIES and allow_gpu_fallback:
            fallback_family = 'lightgbm_auto' if model_family == 'lightgbm_gpu' else 'hist_gbdt'
            model, realized_family, model_meta = build_model(fallback_family, generated_model_path=generated_model_path, model_options={})
            model_meta['gpu_fallback_reason'] = str(exc)
            model.fit(X_train, y_train, **fit_kwargs)
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
    pred_valid_df[effective_label_col] = y_valid.to_numpy()

    pred_test_df = _project_runtime_frame(test_df, code_col=code_col, label_col=label_col, extra_cols=[effective_label_col])
    pred_test_df['pred_score'] = pred_test

    latest_date = df[date_col].max()
    latest_source_df = df.loc[df[date_col] == latest_date].copy()
    X_latest = _clean_feature_frame(latest_source_df, selected)
    latest_df = _project_runtime_frame(latest_source_df, code_col=code_col, label_col=label_col)
    latest_df['pred_score'] = _predict_scores(model=model, X=X_latest, realized_family=realized_family)

    valid_metrics = summarize_prediction_frame(pred_valid_df, date_col=date_col, pred_col='pred_score', label_col=effective_label_col)
    test_metrics = summarize_prediction_frame(pred_test_df, date_col=date_col, pred_col='pred_score', label_col=effective_label_col)
    overfit_diagnostics = build_overfit_diagnostics(
        pred_valid_df,
        pred_test_df,
        date_col=date_col,
        pred_col='pred_score',
        label_col=effective_label_col,
    )

    resource_meta.update(model_meta)
    resource_meta.update({
        'effective_model_family': realized_family,
        'effective_label_col': effective_label_col,
        'selected_feature_count': int(len(selected)),
        'latest_date': str(latest_date),
        'label_horizon': int(label_horizon),
        'execution_lag_bars': int(execution_lag_bars),
    })

    train_summary = {
        'strategy_name': candidate['strategy_name'],
        'label_col': label_col,
        'effective_label_col': effective_label_col,
        'model_family': model_family,
        'effective_model_family': realized_family,
        'feature_profile': candidate['feature_profile'],
        'training_logic': candidate['training_logic'],
        'n_features': int(len(selected)),
        'valid_metrics': valid_metrics,
        'test_metrics': test_metrics,
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
