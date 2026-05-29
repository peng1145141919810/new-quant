# -*- coding: utf-8 -*-
"""模型家族工厂。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.preprocessing import StandardScaler


class FormulaModel:
    """基于公式的轻量模型。"""

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = dict(weights or {})
        self._features = []

    def fit(self, X, y, sample_weight=None):
        self._features = list(X.columns)
        if not self.weights:
            import pandas as pd
            y_ser = pd.Series(y)
            scores = {}
            for c in self._features[:20]:
                try:
                    scores[c] = float(pd.Series(X[c]).corr(y_ser) or 0.0)
                except Exception:
                    scores[c] = 0.0
            self.weights = scores
        return self

    def predict(self, X):
        arr = np.zeros(len(X), dtype=float)
        for c, w in self.weights.items():
            if c in X.columns:
                arr += float(w) * np.asarray(X[c], dtype=float)
        return arr


class StableRidgeRanker:
    """带稳定化预处理的 Ridge 模型。"""

    def __init__(
        self,
        alpha: float = 3.0,
        corr_threshold: float = 0.9995,
        var_threshold: float = 1e-12,
        corr_sample_rows: int = 4096,
        random_state: int = 42,
    ):
        self.alpha = float(alpha)
        self.corr_threshold = float(corr_threshold)
        self.var_threshold = float(var_threshold)
        self.corr_sample_rows = int(corr_sample_rows)
        self.random_state = int(random_state)
        self.scaler = StandardScaler()
        self.model = Ridge(alpha=self.alpha, solver='svd', random_state=self.random_state)
        self.selected_features_: list[str] = []
        self.meta_: Dict[str, Any] = {}

    @staticmethod
    def _to_frame(X: Any) -> pd.DataFrame:
        if isinstance(X, pd.DataFrame):
            frame = X.copy()
        else:
            arr = np.asarray(X, dtype=float)
            frame = pd.DataFrame(arr, columns=[f'f_{idx:03d}' for idx in range(arr.shape[1])])
        return frame.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)

    def _sample_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        if len(frame) <= self.corr_sample_rows:
            return frame
        indices = np.linspace(0, len(frame) - 1, num=self.corr_sample_rows, dtype=int)
        return frame.iloc[indices].copy()

    def fit(self, X, y, sample_weight=None):
        frame = self._to_frame(X)
        original_features = list(frame.columns)
        variances = frame.var(axis=0)
        keep_features = variances.loc[variances > self.var_threshold].index.tolist()
        dropped_constant = [col for col in original_features if col not in keep_features]
        if keep_features:
            frame = frame[keep_features]
        else:
            keep_features = original_features[:1]
            frame = frame[keep_features]
            dropped_constant = [col for col in original_features if col not in keep_features]

        dropped_correlated: list[str] = []
        if frame.shape[1] > 1:
            sampled = self._sample_frame(frame)
            corr = sampled.corr().abs().fillna(0.0)
            upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
            dropped_correlated = [col for col in upper.columns if bool((upper[col] >= self.corr_threshold).any())]
            if len(dropped_correlated) >= len(frame.columns):
                dropped_correlated = dropped_correlated[:-1]
            if dropped_correlated:
                frame = frame.drop(columns=dropped_correlated, errors='ignore')

        if frame.shape[1] == 0:
            frame = self._to_frame(X).iloc[:, :1].copy()
            dropped_constant = []
            dropped_correlated = []

        self.selected_features_ = list(frame.columns)
        x_scaled = self.scaler.fit_transform(frame)
        self.model.fit(x_scaled, y, sample_weight=sample_weight)
        self.meta_ = {
            'linear_original_feature_count': int(len(original_features)),
            'linear_selected_feature_count': int(len(self.selected_features_)),
            'linear_dropped_constant_count': int(len(dropped_constant)),
            'linear_dropped_correlated_count': int(len(dropped_correlated)),
            'linear_dropped_constant_features': dropped_constant[:20],
            'linear_dropped_correlated_features': dropped_correlated[:20],
            'backend': 'stable_ridge',
        }
        return self

    def predict(self, X):
        frame = self._to_frame(X)
        for col in self.selected_features_:
            if col not in frame.columns:
                frame[col] = 0.0
        frame = frame[self.selected_features_]
        x_scaled = self.scaler.transform(frame)
        return self.model.predict(x_scaled)

    def export_meta(self) -> Dict[str, Any]:
        return dict(self.meta_)


def _load_generated_model(path: Path):
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location('generated_model', path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        if hasattr(module, 'build_model'):
            return module.build_model(random_state=42)
    except Exception:
        return None
    return None


def _lightgbm_cpu_model() -> Any:
    try:
        import lightgbm as lgb  # type: ignore
        return lgb.LGBMRegressor(
            objective='regression',
            n_estimators=320,
            learning_rate=0.05,
            num_leaves=31,
            subsample=0.9,
            colsample_bytree=0.9,
            random_state=42,
            n_jobs=-1,
        )
    except Exception:
        return HistGradientBoostingRegressor(max_depth=6, learning_rate=0.05, max_iter=300, random_state=42)


def _lightgbm_gpu_model(model_options: Optional[Dict[str, Any]] = None) -> Any:
    model_options = dict(model_options or {})
    try:
        import lightgbm as lgb  # type: ignore
        # LightGBM GPU cannot accept max_bin > 255.
        max_bin = min(int(model_options.get('max_bin', 63) or 63), 255)
        params: Dict[str, Any] = {
            'objective': 'regression',
            'n_estimators': int(model_options.get('n_estimators', 420)),
            'learning_rate': float(model_options.get('learning_rate', 0.05)),
            'num_leaves': int(model_options.get('num_leaves', 63)),
            'subsample': float(model_options.get('subsample', 0.9)),
            'colsample_bytree': float(model_options.get('colsample_bytree', 0.9)),
            'random_state': 42,
            'n_jobs': -1,
            'device_type': str(model_options.get('lightgbm_device_type', 'gpu')),
            'max_bin': max_bin,
        }
        if 'gpu_platform_id' in model_options:
            params['gpu_platform_id'] = int(model_options['gpu_platform_id'])
        if 'gpu_device_id' in model_options:
            params['gpu_device_id'] = int(model_options['gpu_device_id'])
        if bool(model_options.get('gpu_use_dp', False)):
            params['gpu_use_dp'] = True
        return lgb.LGBMRegressor(**params)
    except Exception:
        return _lightgbm_cpu_model()


def _xgboost_gpu_model(model_options: Optional[Dict[str, Any]] = None) -> Any:
    model_options = dict(model_options or {})
    try:
        import xgboost as xgb  # type: ignore
        device = str(model_options.get('xgboost_device', 'cuda'))
        return xgb.XGBRegressor(
            objective='reg:squarederror',
            n_estimators=int(model_options.get('n_estimators', 420)),
            learning_rate=float(model_options.get('learning_rate', 0.05)),
            max_depth=int(model_options.get('max_depth', 8)),
            subsample=float(model_options.get('subsample', 0.85)),
            colsample_bytree=float(model_options.get('colsample_bytree', 0.85)),
            reg_lambda=float(model_options.get('reg_lambda', 1.0)),
            tree_method=str(model_options.get('tree_method', 'hist')),
            max_bin=int(model_options.get('max_bin', 256)),
            device=device,
            random_state=42,
            n_jobs=0,
        )
    except Exception:
        return HistGradientBoostingRegressor(max_depth=6, learning_rate=0.05, max_iter=300, random_state=42)


def build_model(
    family: str,
    generated_model_path: Optional[Path] = None,
    model_options: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, str, Dict[str, Any]]:
    """构建模型。

    Returns:
        (模型对象, 实际模型家族名, 元信息)
    """
    family = str(family or 'ridge_ranker')
    meta: Dict[str, Any] = {'requested_family': family, 'gpu_requested': family in {'lightgbm_gpu', 'xgboost_gpu'}}

    if family == 'lightgbm_auto':
        model = _lightgbm_cpu_model()
        realized = 'lightgbm_auto' if model.__class__.__name__.startswith('LGBM') else 'hist_gbdt_fallback'
        meta.update({'gpu_used': False, 'backend': realized})
        return model, realized, meta
    if family == 'lightgbm_gpu':
        model = _lightgbm_gpu_model(model_options=model_options)
        realized = 'lightgbm_gpu' if model.__class__.__name__.startswith('LGBM') else 'lightgbm_gpu_fallback_cpu'
        meta.update({'gpu_used': realized == 'lightgbm_gpu', 'backend': realized})
        return model, realized, meta
    if family == 'xgboost_gpu':
        model = _xgboost_gpu_model(model_options=model_options)
        realized = 'xgboost_gpu' if model.__class__.__module__.startswith('xgboost') else 'xgboost_gpu_fallback_cpu'
        meta.update({'gpu_used': realized == 'xgboost_gpu', 'backend': realized})
        return model, realized, meta
    if family == 'ridge_ranker':
        return StableRidgeRanker(alpha=3.0, random_state=42), 'ridge_ranker', {'requested_family': family, 'gpu_used': False, 'backend': 'stable_ridge'}
    if family == 'elastic_net':
        return ElasticNet(alpha=0.001, l1_ratio=0.25, random_state=42), 'elastic_net', {'requested_family': family, 'gpu_used': False, 'backend': 'elastic_net'}
    if family == 'extra_trees':
        return ExtraTreesRegressor(n_estimators=320, max_depth=7, min_samples_leaf=20, random_state=42, n_jobs=-1), 'extra_trees', {'requested_family': family, 'gpu_used': False, 'backend': 'extra_trees'}
    if family == 'hist_gbdt':
        return HistGradientBoostingRegressor(max_depth=6, learning_rate=0.05, max_iter=300, random_state=42), 'hist_gbdt', {'requested_family': family, 'gpu_used': False, 'backend': 'hist_gbdt'}
    if family == 'random_forest':
        return RandomForestRegressor(n_estimators=220, max_depth=8, min_samples_leaf=20, random_state=42, n_jobs=-1), 'random_forest', {'requested_family': family, 'gpu_used': False, 'backend': 'random_forest'}
    if family == 'formula_blend':
        return FormulaModel(), 'formula_blend', {'requested_family': family, 'gpu_used': False, 'backend': 'formula_blend'}
    if family == 'generated_family' and generated_model_path is not None:
        model = _load_generated_model(generated_model_path)
        if model is not None:
            return model, 'generated_family', {'requested_family': family, 'gpu_used': False, 'backend': 'generated_family'}
        return HistGradientBoostingRegressor(max_depth=5, learning_rate=0.05, max_iter=200, random_state=42), 'generated_family_fallback', {'requested_family': family, 'gpu_used': False, 'backend': 'generated_family_fallback'}
    return StableRidgeRanker(alpha=3.0, random_state=42), 'ridge_ranker', {'requested_family': family, 'gpu_used': False, 'backend': 'stable_ridge_default'}
