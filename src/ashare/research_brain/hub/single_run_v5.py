# -*- coding: utf-8 -*-
"""单候选实验执行。"""

from __future__ import annotations

import threading
import time
import gc
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from hub.dataset import load_training_table
from hub.evaluator import compute_total_score
from hub.io_utils import ensure_dir, write_json
from hub.portfolio_engine import backtest_from_pred_test, build_latest_portfolio
from hub.registry import append_record
from hub.training_engine import ResourceBudgetSkip, train_and_predict


class _Heartbeat:
    """长任务心跳日志。"""

    def __init__(self, logger: Any, title: str, interval_seconds: int = 30):
        self.logger = logger
        self.title = title
        self.interval_seconds = max(int(interval_seconds or 30), 5)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_ts = 0.0

    def start(self) -> None:
        self._start_ts = time.time()
        if self.logger is None:
            return

        def _run() -> None:
            while not self._stop.wait(self.interval_seconds):
                elapsed = time.time() - self._start_ts
                self.logger.info('%s 仍在运行中... elapsed=%.1fs', self.title, elapsed)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)


@contextmanager
def _heartbeat(logger: Any, title: str, interval_seconds: int = 30):
    """心跳上下文。"""
    hb = _Heartbeat(logger=logger, title=title, interval_seconds=interval_seconds)
    hb.start()
    try:
        yield
    finally:
        hb.stop()


def execute_single_experiment_v5(
    config: Dict[str, Any],
    dry_run: bool = False,
    logger: Any = None,
    heartbeat_seconds: int = 30,
) -> Dict[str, Any]:
    """执行单个候选实验。

    Args:
        config: 候选实验完整配置。
        dry_run: 是否空跑。
        logger: 日志器。
        heartbeat_seconds: 心跳间隔秒数。

    Returns:
        实验摘要。
    """
    hub_root = Path(str(config['hub_output_root']))
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{config['candidate']['strategy_key'][:8]}"
    run_dir = ensure_dir(hub_root / 'runs' / run_id)
    registry_path = hub_root / 'registry' / 'experiment_registry.csv'
    exp_start = time.time()

    record = {
        'run_id': run_id,
        'cycle_id': config['candidate']['cycle_id'],
        'cycle_index': config['candidate']['cycle_index'],
        'strategy_family_name': config['candidate']['strategy_family_name'],
        'strategy_name': config['candidate']['strategy_name'],
        'strategy_key': config['candidate']['strategy_key'],
        'spec_hash': config['candidate']['spec_hash'],
        'parent_strategy_key': config['candidate']['parent_strategy_key'],
        'research_route': config['candidate']['research_route'],
        'hypothesis': config['candidate']['hypothesis'],
        'feature_profile': config['candidate']['feature_profile'],
        'model_family': config['candidate']['model_family'],
        'training_logic': config['candidate']['training_logic'],
        'label_col': config['candidate']['label_col'],
        'label_horizon': config['candidate']['label_horizon'],
        'top_k': config['candidate']['top_k'],
        'config_path': config['candidate']['config_path'],
        'workspace_dir': config['candidate']['lab']['workspace_dir'],
        'status': 'started',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    if logger is not None:
        logger.info('候选实验启动。run_id=%s workspace=%s', run_id, record['workspace_dir'])

    if dry_run:
        record.update({'status': 'dry_run', 'total_score': 0.0, 'error_message': ''})
        append_record(registry_path, record)
        write_json(run_dir / 'run_summary.json', {'record': record, 'dry_run': True})
        return {'run_id': run_id, 'record': record, 'run_dir': str(run_dir), 'elapsed_seconds': time.time() - exp_start}

    try:
        if logger is not None:
            logger.info('加载训练表开始。data_root=%s', config['train_table_dir'])
        load_start = time.time()
        with _heartbeat(logger, '加载训练表', heartbeat_seconds):
            bundle = load_training_table(
                data_root=Path(str(config['train_table_dir'])),
                label_col=str(config['candidate']['label_col']),
                max_files=int(config.get('data_io', {}).get('max_files', 0) or 0) or None,
                sample_rows=int(config.get('data_io', {}).get('sample_rows', 0) or 0) or None,
            )
        if logger is not None:
            logger.info('加载训练表完成。rows=%s features=%s elapsed=%.1fs', len(bundle.df), len(bundle.feature_cols), time.time() - load_start)

        if logger is not None:
            logger.info('训练与预测开始。model=%s feature=%s logic=%s', config['candidate']['model_family'], config['candidate']['feature_profile'], config['candidate']['training_logic'])
        train_start = time.time()
        with _heartbeat(logger, '训练与预测', heartbeat_seconds):
            train_result = train_and_predict(bundle=bundle, candidate=config['candidate'], run_dir=run_dir)
        del bundle
        gc.collect()
        if logger is not None:
            logger.info('训练与预测完成。n_features=%s effective_model=%s elapsed=%.1fs', train_result['train_summary'].get('n_features', 0), train_result['train_summary'].get('effective_model_family', ''), time.time() - train_start)

        if logger is not None:
            logger.info('最新组合构建开始。top_k=%s', config['strategy'].get('top_k'))
        latest_start = time.time()
        with _heartbeat(logger, '最新组合构建', heartbeat_seconds):
            latest_scores_df = pd.read_csv(train_result['latest_scores_path'])
            latest_portfolio = build_latest_portfolio(latest_scores_df, strategy=config['strategy'], out_dir=run_dir)
            del latest_scores_df
            gc.collect()
        if logger is not None:
            logger.info('最新组合构建完成。n_names=%s elapsed=%.1fs', latest_portfolio.get('n_names', 0), time.time() - latest_start)

        if logger is not None:
            logger.info('组合回测开始。')
        bt_start = time.time()
        with _heartbeat(logger, '组合回测', heartbeat_seconds):
            portfolio_summary = backtest_from_pred_test(
                pred_test_df=train_result['pred_test_df'],
                strategy=config['strategy'],
                out_dir=run_dir,
                label_col=str(train_result['train_summary'].get('effective_label_col', config['candidate']['label_col'])),
            )
        if logger is not None:
            logger.info('组合回测完成。ann=%.4f sharpe=%.4f mdd=%.4f elapsed=%.1fs', float(portfolio_summary.get('annualized_ret', 0.0)), float(portfolio_summary.get('sharpe', 0.0)), float(portfolio_summary.get('max_drawdown', 0.0)), time.time() - bt_start)

        elapsed_seconds = float(time.time() - exp_start)
        total_score = compute_total_score(
            train_result['train_summary'],
            portfolio_summary,
            config.get('evaluation_rules', {}),
            resource_meta=train_result.get('resource_meta', {}),
            elapsed_seconds=elapsed_seconds,
        )
        resource_meta = dict(train_result.get('resource_meta', {}))
        record.update({
            'train_summary_path': train_result['train_summary_path'],
            'portfolio_summary_path': str(run_dir / 'portfolio_summary.json'),
            'latest_portfolio_path': str(run_dir / 'latest_portfolio_v1.csv'),
            'latest_scores_path': train_result['latest_scores_path'],
            'pred_test_path': train_result['pred_test_path'],
            'feature_importance_path': train_result['feature_importance_path'],
            'annualized_ret': float(portfolio_summary.get('annualized_ret', 0.0)),
            'sharpe': float(portfolio_summary.get('sharpe', 0.0)),
            'max_drawdown': float(portfolio_summary.get('max_drawdown', 0.0)),
            'valid_ic': float(train_result['train_summary']['valid_metrics'].get('daily_rank_ic_mean', 0.0)),
            'test_ic': float(train_result['train_summary']['test_metrics'].get('daily_rank_ic_mean', 0.0)),
            'valid_spearman': float(train_result['train_summary']['valid_metrics'].get('spearman_corr', 0.0)),
            'test_spearman': float(train_result['train_summary']['test_metrics'].get('spearman_corr', 0.0)),
            'total_score': float(total_score),
            'n_features': int(train_result['train_summary'].get('n_features', 0)),
            'effective_model_family': str(train_result['train_summary'].get('effective_model_family', config['candidate']['model_family'])),
            'budget_action': str(resource_meta.get('budget_action', 'full_train')),
            'estimated_cost_units': float(resource_meta.get('estimated_cost_units_after_sampling', 0.0) or 0.0),
            'gpu_used': bool(resource_meta.get('gpu_used', False)),
            'status': 'ok',
            'error_message': '',
            'elapsed_seconds': elapsed_seconds,
        })
        append_record(registry_path, record)
        latest_portfolio_meta = {k: v for k, v in latest_portfolio.items() if k != 'latest_portfolio_df'}
        write_json(run_dir / 'run_summary.json', {
            'record': record,
            'latest_portfolio': latest_portfolio_meta,
            'portfolio_summary': portfolio_summary,
            'resource_meta': resource_meta,
        })
        if logger is not None:
            logger.info('候选实验完成。run_id=%s total_score=%.2f total_elapsed=%.1fs budget_action=%s gpu_used=%s', run_id, float(record['total_score']), float(record['elapsed_seconds']), record['budget_action'], record['gpu_used'])
        return {'run_id': run_id, 'record': record, 'run_dir': str(run_dir), 'elapsed_seconds': float(record['elapsed_seconds'])}

    except ResourceBudgetSkip as exc:
        elapsed_seconds = float(time.time() - exp_start)
        meta = dict(exc.meta or {})
        record.update({
            'status': 'skipped_budget_guard',
            'total_score': -50.0,
            'error_message': str(exc),
            'budget_action': str(meta.get('budget_action', 'skip_large_dataset')),
            'estimated_cost_units': float(meta.get('estimated_cost_units_after_sampling', 0.0) or 0.0),
            'elapsed_seconds': elapsed_seconds,
        })
        append_record(registry_path, record)
        write_json(run_dir / 'run_summary.json', {'record': record, 'resource_meta': meta, 'error': str(exc)})
        if logger is not None:
            logger.warning('候选实验被预算护栏跳过。run_id=%s elapsed=%.1fs reason=%s', run_id, elapsed_seconds, str(exc))
        return {'run_id': run_id, 'record': record, 'run_dir': str(run_dir), 'elapsed_seconds': elapsed_seconds}

    except Exception as exc:
        elapsed_seconds = float(time.time() - exp_start)
        record.update({'status': 'failed', 'total_score': -999.0, 'error_message': str(exc), 'elapsed_seconds': elapsed_seconds})
        append_record(registry_path, record)
        write_json(run_dir / 'run_summary.json', {'record': record, 'error': str(exc)})
        if logger is not None:
            logger.exception('候选实验失败。run_id=%s elapsed=%.1fs error=%s', run_id, elapsed_seconds, str(exc))
        return {'run_id': run_id, 'record': record, 'run_dir': str(run_dir), 'elapsed_seconds': elapsed_seconds}
