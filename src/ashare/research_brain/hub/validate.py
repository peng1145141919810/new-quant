# -*- coding: utf-8 -*-
"""本地校验器。"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any, Dict

import numpy as np

from hub.dataset import load_training_table


def _probe_gpu() -> Dict[str, Any]:
    """探测本机 GPU 环境。"""
    payload: Dict[str, Any] = {'ok': False, 'detail': '', 'name': '', 'memory': '', 'driver': ''}
    try:
        proc = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0:
            line = (proc.stdout.strip().splitlines() or [''])[0]
            parts = [x.strip() for x in line.split(',')]
            payload.update({
                'ok': True,
                'detail': line,
                'name': parts[0] if len(parts) > 0 else '',
                'memory': parts[1] if len(parts) > 1 else '',
                'driver': parts[2] if len(parts) > 2 else '',
            })
    except Exception as exc:
        payload['detail'] = str(exc)
    return payload


def _probe_python_package(name: str) -> Dict[str, Any]:
    """探测 Python 包是否可导入。"""
    try:
        module = importlib.import_module(name)
        return {'ok': True, 'version': getattr(module, '__version__', 'unknown')}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def _xgboost_gpu_fit_smoke() -> Dict[str, Any]:
    """做一次极小规模 XGBoost GPU 冒烟训练。"""
    try:
        import xgboost as xgb  # type: ignore
        X = np.random.RandomState(42).randn(256, 8)
        y = np.random.RandomState(7).randn(256)
        model = xgb.XGBRegressor(
            objective='reg:squarederror',
            n_estimators=8,
            max_depth=4,
            learning_rate=0.1,
            tree_method='hist',
            device='cuda',
            random_state=42,
            n_jobs=0,
        )
        model.fit(X, y)
        pred = model.predict(X[:8])
        return {'ok': True, 'detail': f'pred_mean={float(np.mean(pred)):.6f}'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def validate_environment(config: Dict[str, Any]) -> Dict[str, Any]:
    """校验配置与数据。"""
    result = {'ok': True, 'checks': []}
    data_root = Path(str(config['train_table_dir']))
    if not data_root.exists():
        result['ok'] = False
        result['checks'].append({'name': 'train_table_dir', 'ok': False, 'detail': f'不存在: {data_root}'})
        return result
    bundle = load_training_table(data_root=data_root, label_col=str(config['strategy']['label_col']), max_files=1)
    result['checks'].append({'name': 'data_load', 'ok': True, 'detail': f'rows={len(bundle.df)} features={len(bundle.feature_cols)}'})

    gpu_info = _probe_gpu()
    result['checks'].append({'name': 'nvidia_smi', 'ok': gpu_info['ok'], 'detail': gpu_info['detail'], 'name_detected': gpu_info['name'], 'memory': gpu_info['memory'], 'driver': gpu_info['driver']})
    result['checks'].append({'name': 'xgboost_import', **_probe_python_package('xgboost')})
    result['checks'].append({'name': 'lightgbm_import', **_probe_python_package('lightgbm')})
    result['checks'].append({'name': 'xgboost_gpu_fit', **_xgboost_gpu_fit_smoke()})
    result['ok'] = all(bool(item.get('ok', False)) for item in result['checks'] if item['name'] in {'data_load', 'nvidia_smi', 'xgboost_import', 'xgboost_gpu_fit'})
    return result
