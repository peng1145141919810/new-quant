# -*- coding: utf-8 -*-
from __future__ import annotations
import json, os, subprocess, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict
import pandas as pd
from .config_utils import ensure_dir, load_config
from .execution_bridge_runner import (
    build_execution_runtime_config as materialize_execution_runtime_config,
    execution_policy,
    run_execution_bridge as dispatch_execution_bridge,
)
from .data_consistency_guard import assess_automation_data_readiness
from .local_augmentations import build_v5_cycle_review, emit_runtime_stage_note
from .logging_utils import log_line
from .market_state import build_market_state_artifacts, load_latest_market_state
from .market_pipeline import run_market_pipeline
from .objective_scheduler import (
    build_research_budget_decision,
    load_scheduler_signal_context,
    merge_signals_with_budget_feedback,
)
from .portfolio_release import publish_portfolio_release
from .clock_supervisor import run_pre_research_refresh_bundle


def _now_text() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _check_gpu_requirement(config: Dict[str, Any]) -> None:
    if not bool(config.get('supervisor', {}).get('require_gpu', False)):
        return
    try:
        import xgboost as xgb
        _ = xgb.__version__
    except Exception as exc:
        raise RuntimeError('未检测到 xgboost，GPU 训练链路无法启动。') from exc

def _stamp_path(config: Dict[str, Any]) -> Path:
    root = Path(str(config['paths']['bridge_root']))
    ensure_dir(root)
    return root / 'last_token_plan.json'

def _should_run_token_plan(config: Dict[str, Any]) -> bool:
    stamp = _stamp_path(config)
    if not stamp.exists():
        return True
    try:
        payload = json.loads(stamp.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(str(payload.get('timestamp')))
    except Exception:
        return True
    raw_hours = config.get('supervisor', {}).get('token_plan_min_interval_hours', 24)
    hours = 24.0 if raw_hours in (None, "") else float(raw_hours)
    return datetime.now() - ts >= timedelta(hours=hours)

def _write_stamp(config: Dict[str, Any]) -> None:
    _stamp_path(config).write_text(json.dumps({'timestamp': datetime.now().isoformat()}, ensure_ascii=False, indent=2), encoding='utf-8')


def _strategy_feedback_paths(config: Dict[str, Any]) -> Dict[str, Path]:
    bridge_root = ensure_dir(Path(str(config['paths']['bridge_root'])))
    supervisor_root = ensure_dir(Path(str(config['paths']['research_root'])) / 'supervisor')
    return {
        'bridge': bridge_root / 'performance_feedback.json',
        'supervisor': supervisor_root / 'performance_feedback.json',
    }


def _default_strategy_feedback(config: Dict[str, Any], equity_curve_path: Path) -> Dict[str, Any]:
    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'available': False,
        'authority_role': 'local_signal_only',
        'source_equity_curve': str(equity_curve_path),
        'regime': 'neutral',
        'metrics': {},
        'signal_trace': [],
        'signal_route_weights': {},
        'route_space_signals': {},
        'preferred_model_signals': [],
        'ban_model_signals': [],
        'research_constraint_signals': {},
        'portfolio_constraint_signals': {},
        'route_weights': {},
        'route_budget': {},
        'route_space_overrides': {},
        'preferred_model_families': [],
        'ban_model_families': [],
        'strategy_overrides': {},
        'portfolio_overrides': {},
    }


def _build_strategy_feedback(config: Dict[str, Any]) -> Dict[str, Any]:
    equity_curve_path = Path(str(config['paths'].get('live_execution_root', ''))) / 'equity_curve.csv'
    feedback = _default_strategy_feedback(config=config, equity_curve_path=equity_curve_path)
    dynamic_cfg = dict(config.get('dynamic_strategy', {}) or {})
    if not bool(dynamic_cfg.get('enabled', False)) or not equity_curve_path.exists():
        return feedback
    try:
        curve = pd.read_csv(equity_curve_path)
    except Exception:
        return feedback
    if curve.empty or 'nav' not in curve.columns:
        return feedback
    curve = curve.copy()
    ts_col = 'timestamp' if 'timestamp' in curve.columns else curve.columns[0]
    curve[ts_col] = pd.to_datetime(curve[ts_col], errors='coerce')
    curve['nav'] = pd.to_numeric(curve['nav'], errors='coerce')
    curve = curve.dropna(subset=[ts_col, 'nav']).sort_values(ts_col).reset_index(drop=True)
    lookback_days = int(dynamic_cfg.get('lookback_days', 5) or 5)
    curve = curve.tail(max(lookback_days + 1, 2)).reset_index(drop=True)
    if curve.empty:
        return feedback

    latest_nav = float(curve['nav'].iloc[-1])
    prev_nav = float(curve['nav'].iloc[-2]) if len(curve) >= 2 else latest_nav
    day_ret = latest_nav / prev_nav - 1.0 if prev_nav > 0 else 0.0
    ret_3d = latest_nav / float(curve['nav'].iloc[-4]) - 1.0 if len(curve) >= 4 and float(curve['nav'].iloc[-4]) > 0 else day_ret
    ret_5d = latest_nav / float(curve['nav'].iloc[-6]) - 1.0 if len(curve) >= 6 and float(curve['nav'].iloc[-6]) > 0 else ret_3d
    rolling_peak = pd.to_numeric(curve['nav'], errors='coerce').cummax()
    current_drawdown = latest_nav / float(rolling_peak.iloc[-1]) - 1.0 if float(rolling_peak.iloc[-1]) > 0 else 0.0

    def_day = float(dynamic_cfg.get('defensive_daily_return_threshold', -0.02) or -0.02)
    def_3d = float(dynamic_cfg.get('defensive_three_day_return_threshold', -0.03) or -0.03)
    agg_day = float(dynamic_cfg.get('aggressive_daily_return_threshold', 0.015) or 0.015)
    agg_3d = float(dynamic_cfg.get('aggressive_three_day_return_threshold', 0.02) or 0.02)

    regime = 'neutral'
    if day_ret <= def_day or ret_3d <= def_3d or current_drawdown <= -0.05:
        regime = 'defensive'
    elif day_ret >= agg_day and ret_3d >= agg_3d and current_drawdown > -0.03:
        regime = 'aggressive'

    if regime == 'defensive':
        route_weights = {'risk': 0.28, 'portfolio': 0.2, 'data': 0.18, 'model': 0.12, 'feature': 0.1, 'training': 0.07, 'hybrid': 0.05}
        route_space_overrides = {
            'top_ks': [10, 15, 20],
            'base_exposures': [0.75, 0.65, 0.55],
            'weak_exposures': [0.25, 0.2, 0.15],
            'model_families': ['ridge_ranker', 'lightgbm_gpu', 'xgboost_gpu'],
        }
        preferred_models = ['ridge_ranker', 'lightgbm_gpu', 'xgboost_gpu']
        ban_models = ['generated_family']
        strategy_constraints = {
            'top_k': 15,
            'portfolio_base_exposure': 0.75,
            'portfolio_weak_market_exposure': 0.25,
            'portfolio_single_name_cap': 0.08,
        }
        portfolio_constraints = {'max_names': 12, 'single_name_cap': 0.08, 'total_exposure_cap': 0.85}
        signal_trace = ['regime=defensive', 'route_tilt=risk_portfolio_data', 'constraint_bias=deleveraging']
    elif regime == 'aggressive':
        route_weights = {'model': 0.24, 'feature': 0.2, 'training': 0.18, 'hybrid': 0.15, 'portfolio': 0.1, 'risk': 0.07, 'data': 0.06}
        route_space_overrides = {
            'top_ks': [15, 20, 25],
            'base_exposures': [1.0, 0.95, 0.9],
            'weak_exposures': [0.55, 0.45, 0.35],
            'model_families': ['xgboost_gpu', 'lightgbm_gpu', 'ridge_ranker'],
        }
        preferred_models = ['xgboost_gpu', 'lightgbm_gpu', 'ridge_ranker']
        ban_models = []
        strategy_constraints = {
            'top_k': 20,
            'portfolio_base_exposure': 1.0,
            'portfolio_weak_market_exposure': 0.55,
            'portfolio_single_name_cap': 0.1,
        }
        portfolio_constraints = {'max_names': 20, 'single_name_cap': 0.1, 'total_exposure_cap': 1.0}
        signal_trace = ['regime=aggressive', 'route_tilt=model_feature_training', 'constraint_bias=deploy_capital']
    else:
        route_weights = {'feature': 0.18, 'model': 0.18, 'training': 0.16, 'portfolio': 0.16, 'risk': 0.14, 'data': 0.1, 'hybrid': 0.08}
        route_space_overrides = {
            'top_ks': [15, 20, 30],
            'base_exposures': [1.0, 0.9, 0.8],
            'weak_exposures': [0.5, 0.4, 0.3],
        }
        preferred_models = ['xgboost_gpu', 'ridge_ranker', 'lightgbm_gpu']
        ban_models = []
        strategy_constraints = {
            'top_k': 20,
            'portfolio_base_exposure': 1.0,
            'portfolio_weak_market_exposure': 0.5,
            'portfolio_single_name_cap': 0.1,
        }
        portfolio_constraints = {'max_names': 20, 'single_name_cap': 0.1, 'total_exposure_cap': 1.0}
        signal_trace = ['regime=neutral', 'route_tilt=balanced', 'constraint_bias=baseline']

    feedback.update(
        {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'available': True,
            'authority_role': 'local_signal_only',
            'regime': regime,
            'signal_trace': signal_trace + [
                f"daily_return={day_ret:.4f}",
                f"three_day_return={ret_3d:.4f}",
                f"five_day_return={ret_5d:.4f}",
                f"current_drawdown={current_drawdown:.4f}",
            ],
            'metrics': {
                'latest_nav': latest_nav,
                'daily_return': day_ret,
                'three_day_return': ret_3d,
                'five_day_return': ret_5d,
                'current_drawdown': current_drawdown,
                'samples': int(len(curve)),
            },
            'signal_route_weights': route_weights,
            'route_space_signals': route_space_overrides,
            'preferred_model_signals': preferred_models,
            'ban_model_signals': ban_models,
            'research_constraint_signals': strategy_constraints,
            'portfolio_constraint_signals': portfolio_constraints,
            'route_weights': {},
            'route_budget': {},
            'route_space_overrides': {},
            'preferred_model_families': [],
            'ban_model_families': [],
            'strategy_overrides': {},
            'portfolio_overrides': {},
        }
    )
    return feedback


def _write_strategy_feedback(config: Dict[str, Any], feedback: Dict[str, Any]) -> None:
    for path in _strategy_feedback_paths(config).values():
        path.write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding='utf-8')


def _write_research_budget_decision(config: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, str]:
    root = ensure_dir(Path(str(config['paths']['research_root'])) / 'supervisor' / 'objective_scheduler')
    stamped_root = ensure_dir(root / datetime.now().strftime('%Y%m%d_%H%M%S'))
    decision_path = stamped_root / 'research_budget_decision.json'
    verdict_path = stamped_root / 'research_scheduler_verdict.json'
    latest_path = root / 'latest_research_budget_decision.json'
    latest_verdict_path = root / 'latest_research_scheduler_verdict.json'
    payload = json.dumps(decision, ensure_ascii=False, indent=2)
    decision_path.write_text(payload, encoding='utf-8')
    verdict_path.write_text(payload, encoding='utf-8')
    latest_path.write_text(payload, encoding='utf-8')
    latest_verdict_path.write_text(payload, encoding='utf-8')
    return {
        'decision_path': str(decision_path),
        'verdict_path': str(verdict_path),
        'latest_path': str(latest_path),
        'latest_verdict_path': str(latest_verdict_path),
    }


def _apply_strategy_feedback(template: Dict[str, Any], feedback: Dict[str, Any]) -> None:
    strategy_override = dict(feedback.get('strategy_overrides', {}) or {})
    route_space_override = dict(feedback.get('route_space_overrides', {}) or {})
    research_brain_override = dict(feedback.get('research_brain_overrides', {}) or {})
    if strategy_override:
        template.setdefault('strategy', {}).update(strategy_override)
    if research_brain_override:
        template.setdefault('research_brain', {}).update(research_brain_override)
    if route_space_override:
        route_space = template.setdefault('route_space', {})
        for key, value in route_space_override.items():
            if value not in (None, []):
                route_space[key] = value
    preferred_models = list(feedback.get('preferred_model_families', []) or [])
    ban_models = set(feedback.get('ban_model_families', []) or [])
    if preferred_models:
        route_space = template.setdefault('route_space', {})
        current = list(route_space.get('model_families', []) or [])
        route_space['model_families'] = [m for m in list(dict.fromkeys(preferred_models + current)) if m not in ban_models]

def _build_v5_gpu_config(config: Dict[str, Any], project_root: Path, feedback: Dict[str, Any] | None = None) -> Path:
    v5_root = project_root / 'research_brain'
    local_template_path = v5_root / 'configs' / 'hub_config.v5_1.local.json'
    example_template_path = v5_root / 'configs' / 'hub_config.v5_1.example.json'
    template_path = local_template_path if local_template_path.exists() else example_template_path
    template = json.loads(template_path.read_text(encoding='utf-8'))
    runtime_cfg = dict(config.get('research_brain', {}))
    template['project_root'] = str(runtime_cfg['project_root'])
    template['train_table_dir'] = str(runtime_cfg['train_table_dir'])
    template['hub_output_root'] = str(runtime_cfg['hub_output_root'])
    template['execution']['python_executable'] = str(runtime_cfg['python_executable'])
    template['research_brain']['max_cycles'] = int(config.get('supervisor', {}).get('v5_gpu_max_cycles_per_tick', 8) or 8)
    template['research_brain']['sleep_seconds'] = 0
    template['llm_brain']['enabled'] = True
    template['llm_brain']['api_key_env'] = str(config['providers']['deepseek_worker']['api_key_env'])
    template['llm_brain']['base_url'] = str(config['providers']['deepseek_worker']['base_url'])
    template['llm_brain']['model'] = str(config['providers']['deepseek_worker']['model'])
    template['llm_brain']['timeout_seconds'] = int(config['providers']['deepseek_worker'].get('timeout_seconds', 90) or 90)
    template['llm_brain']['temperature'] = 0.15
    template['bridge_inputs'] = {'enabled': True, 'bridge_root': str(runtime_cfg['bridge_input_root'])}
    if feedback:
        _apply_strategy_feedback(template, feedback)
    effective_max_cycles = int(template.get('research_brain', {}).get('max_cycles', config.get('supervisor', {}).get('v5_gpu_max_cycles_per_tick', 8)) or 8)
    out_path = v5_root / 'configs' / 'hub_config.v5_1.integrated_gpu.json'
    out_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding='utf-8')
    local_settings = v5_root / 'hub' / 'local_settings.py'
    local_settings.write_text(
        "# -*- coding: utf-8 -*-\nCONFIG_PATH = r\"configs/hub_config.v5_1.integrated_gpu.json\"\nMODE = \"adaptive_research_brain\"\nDRY_RUN = False\nMAX_CYCLES = %d\nSLEEP_SECONDS = 0\n" % effective_max_cycles,
        encoding='utf-8'
    )
    return out_path

def _run_v5_gpu(config: Dict[str, Any], project_root: Path, feedback: Dict[str, Any] | None = None) -> None:
    v5_root = project_root / 'research_brain'
    _build_v5_gpu_config(config, project_root, feedback=feedback)
    pyexe = str(config.get('research_brain', {}).get('python_executable'))
    script = v5_root / 'run_research_hub_v5_1_local.py'
    env = os.environ.copy()
    # 强制 unbuffered stdout/stderr，避免 supervisor 用 `*> $log` 重定向时拿到 0 字节文件。
    env['PYTHONUNBUFFERED'] = '1'
    output_root = str(config.get('research_brain', {}).get('hub_output_root', '') or '')
    log_line(
        config,
        f"Supervisor: V5.1 研究进程已启动，输出根={output_root}，可观察 controller_state.json / registry/experiment_registry.csv / cycles/*/cycle_summary.json",
    )
    subprocess.run([pyexe, str(script)], cwd=str(v5_root), check=True, env=env)

def _write_supervisor_state(config: Dict[str, Any], payload: Dict[str, Any]) -> None:
    root = ensure_dir(Path(str(config['paths']['research_root'])) / 'supervisor')
    payload['updated_at'] = _now_text()
    (root / 'supervisor_state.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _compact_stage_summary(value: Any, max_chars: int = 240) -> str:
    if value in (None, ''):
        return ''
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    return text[:max_chars]


def _stage_bookkeeping(state: Dict[str, Any], stage_name: str, stage_label: str, stage_order: int, stage_total: int, status: str, summary: str = '') -> None:
    stages = dict(state.get('stages', {}) or {})
    stage_payload = dict(stages.get(stage_name, {}) or {})
    if status == 'running' and 'started_at' not in stage_payload:
        stage_payload['started_at'] = _now_text()
    if status in {'completed', 'failed', 'skipped'}:
        stage_payload['completed_at'] = _now_text()
    stage_payload.update({
        'label': stage_label,
        'order': stage_order,
        'total': stage_total,
        'status': status,
    })
    if summary:
        stage_payload['summary'] = summary
    stages[stage_name] = stage_payload
    state['stages'] = stages
    state['current_stage'] = {
        'name': stage_name,
        'label': stage_label,
        'order': stage_order,
        'total': stage_total,
        'status': status,
        'updated_at': _now_text(),
        'summary': summary,
    }
    history = list(state.get('stage_history', []) or [])
    history.append({
        'timestamp': _now_text(),
        'stage': stage_name,
        'label': stage_label,
        'order': stage_order,
        'total': stage_total,
        'status': status,
        'summary': summary,
    })
    state['stage_history'] = history[-50:]


def _stage_start(config: Dict[str, Any], state: Dict[str, Any], stage_name: str, stage_label: str, stage_order: int, stage_total: int) -> None:
    _stage_bookkeeping(state, stage_name, stage_label, stage_order, stage_total, 'running')
    note = emit_runtime_stage_note(config=config, stage_name=stage_name, stage_label=stage_label, status='running')
    if note:
        runtime_notes = list(state.get('runtime_notes', []) or [])
        runtime_notes.append(note)
        state['runtime_notes'] = runtime_notes[-20:]
    _write_supervisor_state(config, state)
    log_line(config, f"Supervisor: [{stage_order}/{stage_total}] {stage_label} 开始。")


def _stage_finish(config: Dict[str, Any], state: Dict[str, Any], stage_name: str, stage_label: str, stage_order: int, stage_total: int, summary: str = '') -> None:
    _stage_bookkeeping(state, stage_name, stage_label, stage_order, stage_total, 'completed', summary=summary)
    _write_supervisor_state(config, state)
    msg = f"Supervisor: [{stage_order}/{stage_total}] {stage_label} 完成。"
    if summary:
        msg += f" {summary}"
    log_line(config, msg)


def _stage_skip(config: Dict[str, Any], state: Dict[str, Any], stage_name: str, stage_label: str, stage_order: int, stage_total: int, summary: str = '') -> None:
    _stage_bookkeeping(state, stage_name, stage_label, stage_order, stage_total, 'skipped', summary=summary)
    _write_supervisor_state(config, state)
    msg = f"Supervisor: [{stage_order}/{stage_total}] {stage_label} 跳过。"
    if summary:
        msg += f" {summary}"
    log_line(config, msg)


def _stage_fail(config: Dict[str, Any], state: Dict[str, Any], stage_name: str, stage_label: str, stage_order: int, stage_total: int, summary: str) -> None:
    _stage_bookkeeping(state, stage_name, stage_label, stage_order, stage_total, 'failed', summary=summary)
    _write_supervisor_state(config, state)
    log_line(config, f"Supervisor: [{stage_order}/{stage_total}] {stage_label} 失败。{summary}")


def _maybe_publish_release(config: Dict[str, Any], state: Dict[str, Any], source_mode: str) -> None:
    release_cfg = dict(config.get('trade_release', {}) or {})
    if not bool(release_cfg.get('enabled', True)):
        state['portfolio_release_skipped'] = 'disabled'
        return
    try:
        release = publish_portfolio_release(
            config=config,
            source_mode=source_mode,
            profile=str(config.get('runtime_selection', {}).get('profile', '') or ''),
        )
        state['portfolio_release'] = {
            'release_id': str(release.get('release_id', '') or ''),
            'trade_date': str(release.get('trade_date', '') or ''),
            'manifest_path': str(release.get('artifacts', {}).get('manifest_path', '') or ''),
        }
        log_line(
            config,
            (
                "Supervisor: 已发布 portfolio release "
                f"release_id={state['portfolio_release'].get('release_id', '')} "
                f"trade_date={state['portfolio_release'].get('trade_date', '')}"
            ),
        )
    except Exception as exc:
        state['portfolio_release_error'] = str(exc)
        log_line(config, f"Supervisor: portfolio release 发布失败：{exc}")


def _supervisor_direct_execution_decision(config: Dict[str, Any]) -> Dict[str, Any]:
    policy = execution_policy(config)
    if str(policy.get('account_mode', 'simulation')) != 'precision':
        return {'allowed': True, 'reason': 'simulation_mode'}
    if not bool(policy.get('precision_trade_enabled', False)):
        return {'allowed': False, 'reason': 'precision_trade_disabled'}
    if not bool(policy.get('allow_integrated_precision_execution', False)):
        return {'allowed': False, 'reason': 'precision_mode_deferred_to_execution_only'}
    return {'allowed': True, 'reason': 'precision_mode_allowed'}

def _build_execution_runtime_config(config: Dict[str, Any]) -> Path:
    return materialize_execution_runtime_config(
        config=config,
        explicit_portfolio_path=str(Path(str(config['paths']['portfolio_output_root'])) / 'target_positions.csv'),
    )

def _run_execution_bridge(config: Dict[str, Any], project_root: Path) -> Dict[str, Any]:
    return dispatch_execution_bridge(
        config=config,
        project_root=project_root,
        explicit_portfolio_path=str(Path(str(config['paths']['portfolio_output_root'])) / 'target_positions.csv'),
    )


def run_resume_downstream(config_path: Path, include_execution: bool = False) -> None:
    """从最近一次已完成的 V5 结果继续，生成持仓建议，并可选重跑执行桥。"""
    config = load_config(config_path)
    project_root = config_path.resolve().parent.parent
    stage_total = 1 + (1 if include_execution and bool(config.get('execution_bridge', {}).get('enabled', False)) else 0)
    state: Dict[str, Any] = {'started_at': _now_text(), 'resume_mode': 'downstream_only'}
    if bool(config.get('portfolio_recommendation', {}).get('enabled', False)):
        from .portfolio_recommendation import build_portfolio_recommendation
        _stage_start(config, state, 'portfolio_recommendation', '断点续跑持仓建议生成', 1, stage_total)
        try:
            rec = build_portfolio_recommendation(config=config, bridge_root=Path(str(config['paths']['bridge_root'])))
            state['portfolio_recommendation'] = rec
            _maybe_publish_release(config=config, state=state, source_mode='resume_downstream')
            _stage_finish(
                config,
                state,
                'portfolio_recommendation',
                '断点续跑持仓建议生成',
                1,
                stage_total,
                summary=(
                    f"run_id={rec.get('run_id')} n_names={rec.get('n_names')} "
                    f"regime={rec.get('market_regime', '')} tech_allow={rec.get('tech_allow_count', 0)}"
                ),
            )
        except Exception as exc:
            state['portfolio_recommendation_error'] = str(exc)
            _stage_fail(config, state, 'portfolio_recommendation', '断点续跑持仓建议生成', 1, stage_total, summary=str(exc))
            raise
    else:
        state['portfolio_recommendation_skipped'] = 'disabled'
        _stage_skip(config, state, 'portfolio_recommendation', '断点续跑持仓建议生成', 1, stage_total, summary='disabled')

    if include_execution and bool(config.get('execution_bridge', {}).get('enabled', False)):
        _stage_start(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total)
        summary_path = Path(str(config['paths']['portfolio_output_root'])) / 'portfolio_recommendation.json'
        should_trade = True
        direct_exec = _supervisor_direct_execution_decision(config)
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            should_trade = bool(summary.get('simulation_ready', True))
        if not bool(direct_exec.get('allowed', False)):
            state['execution_bridge_skipped'] = str(direct_exec.get('reason', 'direct_execution_blocked'))
            _stage_skip(
                config,
                state,
                'execution_bridge',
                '断点续跑执行桥',
                stage_total,
                stage_total,
                summary=str(direct_exec.get('reason', 'direct_execution_blocked')),
            )
        elif should_trade:
            try:
                state['execution_bridge'] = _run_execution_bridge(config=config, project_root=project_root)
                _stage_finish(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total, summary='execution_bridge_completed')
            except Exception as exc:
                state['execution_bridge_error'] = str(exc)
                _stage_fail(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total, summary=str(exc))
                raise
        else:
            state['execution_bridge_skipped'] = 'simulation_ready_false'
            _stage_skip(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total, summary='simulation_ready_false')
    elif include_execution:
        state['execution_bridge_skipped'] = 'disabled'
        _stage_skip(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total, summary='disabled')
    else:
        state['execution_bridge_skipped'] = 'resume_without_execution'
        if stage_total > 1:
            _stage_skip(config, state, 'execution_bridge', '断点续跑执行桥', stage_total, stage_total, summary='resume_without_execution')
    _write_supervisor_state(config, state)

def run_integrated_supervisor(
    config_path: Path,
    run_mode_label: str = 'integrated_supervisor',
    release_source_mode: str = '',
) -> None:
    config = load_config(config_path)
    project_root = config_path.resolve().parent.parent
    _check_gpu_requirement(config)
    effective_release_source_mode = str(release_source_mode or run_mode_label or 'integrated_supervisor')
    sup = dict(config.get('supervisor', {}))
    max_ticks = int(sup.get('max_ticks', 1) or 1)
    run_forever = bool(sup.get('run_forever', False))
    sleep_seconds = int(sup.get('sleep_seconds', 300) or 300)
    evidence_cfg = dict(config.get('evidence_audit', {}) or {})
    evidence_run_after_portfolio = bool(evidence_cfg.get('enabled', True)) and bool(evidence_cfg.get('run_after_portfolio_recommendation', False))
    evidence_rebuild_portfolio = bool(evidence_cfg.get('rebuild_portfolio_after_audit', False))
    stage_defs = []
    stage_defs.append(('pre_research_refresh', '研究前数据刷新'))
    if bool(config.get('market_pipeline', {}).get('enabled', False)):
        stage_defs.append(('market_pipeline', '市场数据流水线'))
    stage_defs.append(('strategy_feedback', '策略信号刷新'))
    stage_defs.append(('objective_scheduler', '中枢研究预算决策'))
    stage_defs.append(('v6_planning', 'V6 研究计划'))
    stage_defs.append(('v5_gpu', 'V5.1 GPU 研究'))
    if bool(config.get('portfolio_recommendation', {}).get('enabled', False)):
        stage_defs.append(('portfolio_recommendation', '持仓建议生成'))
    if evidence_run_after_portfolio:
        stage_defs.append(('evidence_audit', 'small-pool evidence audit'))
    if bool(config.get('execution_bridge', {}).get('enabled', False)):
        stage_defs.append(('execution_bridge', '执行桥'))
    stage_total = len(stage_defs)
    stage_order_map = {name: idx for idx, (name, _) in enumerate(stage_defs, start=1)}
    stage_label_map = {name: label for name, label in stage_defs}
    tick = 0
    while True:
        tick += 1
        state: Dict[str, Any] = {'tick': tick, 'started_at': _now_text(), 'run_mode': str(run_mode_label or 'integrated_supervisor')}
        portfolio_ready = not bool(config.get('portfolio_recommendation', {}).get('enabled', False))
        _stage_start(config, state, 'pre_research_refresh', stage_label_map['pre_research_refresh'], stage_order_map['pre_research_refresh'], stage_total)
        try:
            pre_research_refresh = run_pre_research_refresh_bundle(
                config=config,
                trade_date=datetime.now().strftime('%Y-%m-%d'),
            )
            state['pre_research_refresh'] = pre_research_refresh
            blocking_failure = dict(pre_research_refresh.get('blocking_failure', {}) or {})
            failed_but_open = list(pre_research_refresh.get('failed_but_open', []) or [])
            if blocking_failure:
                summary = f"blocking={blocking_failure.get('name', 'unknown')}"
                _stage_fail(
                    config,
                    state,
                    'pre_research_refresh',
                    stage_label_map['pre_research_refresh'],
                    stage_order_map['pre_research_refresh'],
                    stage_total,
                    summary=summary,
                )
                log_line(config, f"Supervisor: 研究前数据刷新阻断，{summary}")
                _write_supervisor_state(config, state)
                if (not run_forever) and tick >= max_ticks:
                    break
                time.sleep(sleep_seconds)
                continue
            data_readiness = assess_automation_data_readiness(
                config=config,
                trade_date=datetime.now().strftime('%Y-%m-%d'),
                phase_name='research',
            )
            state['pre_research_refresh']['data_consistency_gate'] = data_readiness
            gate_cfg = dict(dict(config.get('trade_clock', {}) or {}).get('scheduler', {}).get('data_consistency_gate', {}) or {})
            if data_readiness.get('enabled', False) and not data_readiness.get('ok', True) and not bool(gate_cfg.get('fail_open', False)):
                summary = f"data_gate={','.join(list(data_readiness.get('issues', []) or []))}"
                _stage_fail(
                    config,
                    state,
                    'pre_research_refresh',
                    stage_label_map['pre_research_refresh'],
                    stage_order_map['pre_research_refresh'],
                    stage_total,
                    summary=summary,
                )
                log_line(config, f"Supervisor: 研究前数据一致性闸门阻断，{summary}")
                _write_supervisor_state(config, state)
                if (not run_forever) and tick >= max_ticks:
                    break
                time.sleep(sleep_seconds)
                continue
            summary = (
                f"warnings={int(pre_research_refresh.get('warning_count', 0) or 0)} "
                f"fail_open={','.join(failed_but_open) if failed_but_open else 'none'}"
            )
            _stage_finish(
                config,
                state,
                'pre_research_refresh',
                stage_label_map['pre_research_refresh'],
                stage_order_map['pre_research_refresh'],
                stage_total,
                summary=summary,
            )
        except Exception as exc:
            state['pre_research_refresh_error'] = str(exc)
            _stage_fail(
                config,
                state,
                'pre_research_refresh',
                stage_label_map['pre_research_refresh'],
                stage_order_map['pre_research_refresh'],
                stage_total,
                summary=str(exc),
            )
            log_line(config, f"Supervisor: 研究前数据刷新失败：{exc}")
            raise
        if bool(config.get('market_pipeline', {}).get('enabled', False)):
            _stage_start(config, state, 'market_pipeline', stage_label_map['market_pipeline'], stage_order_map['market_pipeline'], stage_total)
            try:
                market_pipeline_refresh = dict(
                    dict(state.get('pre_research_refresh', {}) or {}).get('market_pipeline_refresh', {}) or {}
                )
                if bool(market_pipeline_refresh.get('ran', False)):
                    reused_payload = dict(market_pipeline_refresh.get('result_payload', {}) or {})
                    reused_payload['_reused_from_pre_research_refresh'] = True
                    reused_payload['_refresh_message'] = str(market_pipeline_refresh.get('message', '') or '')
                    state['market_pipeline'] = reused_payload
                else:
                    state['market_pipeline'] = run_market_pipeline(config=config)
                _stage_finish(
                    config,
                    state,
                    'market_pipeline',
                    stage_label_map['market_pipeline'],
                    stage_order_map['market_pipeline'],
                    stage_total,
                    summary=_compact_stage_summary(state['market_pipeline']),
                )
            except Exception as exc:
                state['market_pipeline_error'] = str(exc)
                _stage_fail(
                    config,
                    state,
                    'market_pipeline',
                    stage_label_map['market_pipeline'],
                    stage_order_map['market_pipeline'],
                    stage_total,
                    summary=str(exc),
                )
                log_line(config, f"Supervisor: 市场数据流水线失败：{exc}")
        _stage_start(config, state, 'strategy_feedback', stage_label_map['strategy_feedback'], stage_order_map['strategy_feedback'], stage_total)
        try:
            strategy_signals = _build_strategy_feedback(config)
            state['strategy_feedback'] = strategy_signals
            _stage_finish(
                config,
                state,
                'strategy_feedback',
                stage_label_map['strategy_feedback'],
                stage_order_map['strategy_feedback'],
                stage_total,
                summary=f"regime={strategy_signals.get('regime', 'neutral')}",
            )
        except Exception as exc:
            strategy_signals = _default_strategy_feedback(config=config, equity_curve_path=Path(str(config['paths'].get('live_execution_root', ''))) / 'equity_curve.csv')
            state['strategy_feedback_error'] = str(exc)
            _stage_fail(
                config,
                state,
                'strategy_feedback',
                stage_label_map['strategy_feedback'],
                stage_order_map['strategy_feedback'],
                stage_total,
                summary=str(exc),
            )
            log_line(config, f"Supervisor: 策略反馈生成失败：{exc}")
        _stage_start(config, state, 'objective_scheduler', stage_label_map['objective_scheduler'], stage_order_map['objective_scheduler'], stage_total)
        try:
            market_state_build = dict(build_market_state_artifacts(config=config) or {})
            market_state = dict(market_state_build.get('payload', {}) or {})
            if not market_state:
                market_state = dict(load_latest_market_state(config=config, allow_build=False) or {})
            signal_context = load_scheduler_signal_context(config)
            budget_decision = build_research_budget_decision(
                config=config,
                profile=str(config.get('runtime_selection', {}).get('profile', '') or ''),
                market_state=market_state,
                strategy_signals=strategy_signals,
                signal_context=signal_context,
            )
            feedback = merge_signals_with_budget_feedback(strategy_signals=strategy_signals, budget_decision=budget_decision)
            budget_paths = _write_research_budget_decision(config, budget_decision)
            _write_strategy_feedback(config, feedback)
            state['strategy_feedback'] = feedback
            state['objective_scheduler'] = {
                'budget_decision': budget_decision,
                'artifact_paths': budget_paths,
            }
            _stage_finish(
                config,
                state,
                'objective_scheduler',
                stage_label_map['objective_scheduler'],
                stage_order_map['objective_scheduler'],
                stage_total,
                summary=(
                    f"max_cycles={budget_decision.get('research_brain_overrides', {}).get('max_cycles')} "
                    f"budget={budget_decision.get('research_brain_overrides', {}).get('cycle_candidate_budget')}"
                ),
            )
        except Exception as exc:
            feedback = merge_signals_with_budget_feedback(
                strategy_signals=strategy_signals,
                budget_decision={
                    'route_weights': dict(strategy_signals.get('signal_route_weights', {}) or {}),
                    'route_space_overrides': dict(strategy_signals.get('route_space_signals', {}) or {}),
                    'preferred_model_families': list(strategy_signals.get('preferred_model_signals', []) or []),
                    'ban_model_families': list(strategy_signals.get('ban_model_signals', []) or []),
                    'research_brain_overrides': {},
                    'reasons': ['objective_scheduler_failed_fallback_to_local_signals'],
                },
            )
            _write_strategy_feedback(config, feedback)
            state['strategy_feedback'] = feedback
            state['objective_scheduler_error'] = str(exc)
            _stage_fail(
                config,
                state,
                'objective_scheduler',
                stage_label_map['objective_scheduler'],
                stage_order_map['objective_scheduler'],
                stage_total,
                summary=str(exc),
            )
            log_line(config, f"Supervisor: 中枢预算决策失败：{exc}")
        v6_stage_order = stage_order_map['v6_planning']
        v6_stage_label = stage_label_map['v6_planning']
        if _should_run_token_plan(config):
            from .orchestrator_v6 import run_v6_cycle
            _stage_start(config, state, 'v6_planning', v6_stage_label, v6_stage_order, stage_total)
            try:
                run_v6_cycle(config_path=config_path, mode='full_cycle')
                _write_stamp(config)
                state['v6_ran'] = True
                _stage_finish(config, state, 'v6_planning', v6_stage_label, v6_stage_order, stage_total, summary='full_cycle_completed')
            except Exception as exc:
                state['v6_ran'] = False
                state['v6_error'] = str(exc)
                _stage_fail(config, state, 'v6_planning', v6_stage_label, v6_stage_order, stage_total, summary=str(exc))
                raise
        else:
            state['v6_ran'] = False
            _stage_skip(config, state, 'v6_planning', v6_stage_label, v6_stage_order, stage_total, summary='沿用 24 小时内最近一次研究计划')
        _stage_start(config, state, 'v5_gpu', stage_label_map['v5_gpu'], stage_order_map['v5_gpu'], stage_total)
        try:
            _run_v5_gpu(config, project_root, feedback=feedback)
            state['v5_gpu_completed'] = True
            review_summary = 'review=not_generated'
            try:
                review_result = build_v5_cycle_review(config=config)
                if bool(review_result.get('ok', False)):
                    state['v5_cycle_review'] = dict(review_result.get('review', {}) or {})
                    cycle_id = str(state['v5_cycle_review'].get('cycle_id', '') or '')
                    review_summary = f"review=ok cycle_id={cycle_id}" if cycle_id else 'review=ok'
                else:
                    review_error = str(review_result.get('error', 'review_unavailable') or 'review_unavailable')
                    state['v5_cycle_review_error'] = review_error
                    review_summary = f"review={review_error}"
                    log_line(config, f"Supervisor: V5 本地复盘未生成 {review_error}")
            except Exception as review_exc:
                state['v5_cycle_review_error'] = str(review_exc)
                review_summary = f"review_error={review_exc}"
                log_line(config, f"Supervisor: V5 本地复盘失败：{review_exc}")
            _stage_finish(
                config,
                state,
                'v5_gpu',
                stage_label_map['v5_gpu'],
                stage_order_map['v5_gpu'],
                stage_total,
                summary=(
                    f"hub_output_root={config.get('research_brain', {}).get('hub_output_root', '')} "
                    f"{review_summary}"
                ),
            )
        except Exception as exc:
            state['v5_gpu_error'] = str(exc)
            _stage_fail(
                config,
                state,
                'v5_gpu',
                stage_label_map['v5_gpu'],
                stage_order_map['v5_gpu'],
                stage_total,
                summary=str(exc),
            )
            raise
        if bool(config.get('portfolio_recommendation', {}).get('enabled', False)):
            from .portfolio_recommendation import build_portfolio_recommendation
            _stage_start(config, state, 'portfolio_recommendation', stage_label_map['portfolio_recommendation'], stage_order_map['portfolio_recommendation'], stage_total)
            try:
                rec = build_portfolio_recommendation(config=config, bridge_root=Path(str(config['paths']['bridge_root'])))
                state['portfolio_recommendation'] = rec
                portfolio_ready = True
                if not (evidence_run_after_portfolio and evidence_rebuild_portfolio):
                    _maybe_publish_release(config=config, state=state, source_mode=effective_release_source_mode)
                _stage_finish(
                    config,
                    state,
                    'portfolio_recommendation',
                    stage_label_map['portfolio_recommendation'],
                    stage_order_map['portfolio_recommendation'],
                    stage_total,
                    summary=(
                        f"run_id={rec.get('run_id')} n_names={rec.get('n_names')} "
                        f"regime={rec.get('market_regime', '')} tech_allow={rec.get('tech_allow_count', 0)}"
                    ),
                )
            except Exception as exc:
                state['portfolio_recommendation_error'] = str(exc)
                portfolio_ready = False
                _stage_fail(
                    config,
                    state,
                    'portfolio_recommendation',
                    stage_label_map['portfolio_recommendation'],
                    stage_order_map['portfolio_recommendation'],
                    stage_total,
                    summary=str(exc),
                )
                log_line(config, f"Supervisor: 持仓建议生成失败：{exc}")
        if evidence_run_after_portfolio:
            _stage_start(config, state, 'evidence_audit', stage_label_map['evidence_audit'], stage_order_map['evidence_audit'], stage_total)
            try:
                if not portfolio_ready:
                    state['evidence_audit_skipped'] = 'portfolio_recommendation_failed'
                    _stage_skip(
                        config,
                        state,
                        'evidence_audit',
                        stage_label_map['evidence_audit'],
                        stage_order_map['evidence_audit'],
                        stage_total,
                        summary='portfolio_recommendation_failed',
                    )
                else:
                    from .evidence_audit import run_evidence_audit
                    from .portfolio_recommendation import build_portfolio_recommendation

                    candidate_pool_path = Path(str(config['paths']['portfolio_output_root'])) / 'candidate_pool.csv'
                    audit_result = run_evidence_audit(config=config, candidate_pool_path=candidate_pool_path)
                    state['evidence_audit'] = audit_result
                    if evidence_rebuild_portfolio and bool(audit_result.get('ok', False)):
                        rec = build_portfolio_recommendation(config=config, bridge_root=Path(str(config['paths']['bridge_root'])))
                        state['portfolio_recommendation'] = rec
                        _maybe_publish_release(config=config, state=state, source_mode=effective_release_source_mode)
                    _stage_finish(
                        config,
                        state,
                        'evidence_audit',
                        stage_label_map['evidence_audit'],
                        stage_order_map['evidence_audit'],
                        stage_total,
                        summary=(
                            f"audited={int(audit_result.get('audited_count', 0) or 0)} "
                            f"sources={int(audit_result.get('source_count', 0) or 0)} "
                            f"rebuild={str(evidence_rebuild_portfolio).lower()}"
                        ),
                    )
            except Exception as exc:
                state['evidence_audit_error'] = str(exc)
                _stage_fail(
                    config,
                    state,
                    'evidence_audit',
                    stage_label_map['evidence_audit'],
                    stage_order_map['evidence_audit'],
                    stage_total,
                    summary=str(exc),
                )
                if bool(evidence_cfg.get('block_execution_on_failure', False)):
                    portfolio_ready = False
                log_line(config, f"Supervisor: small-pool evidence audit failed: {exc}")
        if bool(config.get('execution_bridge', {}).get('enabled', False)):
            _stage_start(config, state, 'execution_bridge', stage_label_map['execution_bridge'], stage_order_map['execution_bridge'], stage_total)
            try:
                if not portfolio_ready:
                    state['execution_bridge_skipped'] = 'portfolio_recommendation_failed'
                    _stage_skip(
                        config,
                        state,
                        'execution_bridge',
                        stage_label_map['execution_bridge'],
                        stage_order_map['execution_bridge'],
                        stage_total,
                        summary='portfolio_recommendation_failed',
                    )
                    log_line(config, 'Supervisor: 持仓建议未成功生成，本轮跳过执行桥以避免沿用旧文件。')
                    _write_supervisor_state(config, state)
                    if (not run_forever) and tick >= max_ticks:
                        break
                    time.sleep(sleep_seconds)
                    continue
                summary_path = Path(str(config['paths']['portfolio_output_root'])) / 'portfolio_recommendation.json'
                should_trade = True
                if summary_path.exists():
                    summary = json.loads(summary_path.read_text(encoding='utf-8'))
                    should_trade = bool(summary.get('simulation_ready', True))
                direct_exec = _supervisor_direct_execution_decision(config)
                if not bool(direct_exec.get('allowed', False)):
                    state['execution_bridge_skipped'] = str(direct_exec.get('reason', 'direct_execution_blocked'))
                    _stage_skip(
                        config,
                        state,
                        'execution_bridge',
                        stage_label_map['execution_bridge'],
                        stage_order_map['execution_bridge'],
                        stage_total,
                        summary=str(direct_exec.get('reason', 'direct_execution_blocked')),
                    )
                elif should_trade:
                    from .execution_manager import run_execution_only

                    state['execution_bridge'] = run_execution_only(
                        config_path=config_path,
                        trigger_label='supervisor',
                        trigger_source='integrated_supervisor',
                        intent_source='integrated_supervisor',
                    )
                    strategy_signals_post_trade = _build_strategy_feedback(config)
                    signal_context_post_trade = load_scheduler_signal_context(config)
                    market_state_post_trade = dict(load_latest_market_state(config=config, allow_build=False) or {})
                    budget_decision_post_trade = build_research_budget_decision(
                        config=config,
                        profile=str(config.get('runtime_selection', {}).get('profile', '') or ''),
                        market_state=market_state_post_trade,
                        strategy_signals=strategy_signals_post_trade,
                        signal_context=signal_context_post_trade,
                    )
                    feedback = merge_signals_with_budget_feedback(
                        strategy_signals=strategy_signals_post_trade,
                        budget_decision=budget_decision_post_trade,
                    )
                    _write_strategy_feedback(config, feedback)
                    _write_research_budget_decision(config, budget_decision_post_trade)
                    state['strategy_feedback_post_trade'] = feedback
                    control_summary = dict(state['execution_bridge'].get('portfolio_control', {}) or {})
                    exec_feedback = dict(control_summary.get('execution_feedback_summary', {}) or {})
                    _stage_finish(
                        config,
                        state,
                        'execution_bridge',
                        stage_label_map['execution_bridge'],
                        stage_order_map['execution_bridge'],
                        stage_total,
                        summary=(
                            f"orders={state['execution_bridge'].get('n_orders', 0)} "
                            f"fills={state['execution_bridge'].get('n_fills', 0)} "
                            f"turnover={float(control_summary.get('final_turnover_ratio', 0.0) or 0.0):.4f} "
                            f"feedback_success={int(exec_feedback.get('n_success', 0) or 0)}"
                        ),
                    )
                else:
                    state['execution_bridge_skipped'] = 'simulation_ready_false'
                    _stage_skip(
                        config,
                        state,
                        'execution_bridge',
                        stage_label_map['execution_bridge'],
                        stage_order_map['execution_bridge'],
                        stage_total,
                        summary='simulation_ready_false',
                    )
            except Exception as exc:
                state['execution_bridge_error'] = str(exc)
                _stage_fail(
                    config,
                    state,
                    'execution_bridge',
                    stage_label_map['execution_bridge'],
                    stage_order_map['execution_bridge'],
                    stage_total,
                    summary=str(exc),
                )
                log_line(config, f"Supervisor: 执行桥失败：{exc}")
        _write_supervisor_state(config, state)
        if (not run_forever) and tick >= max_ticks:
            break
        time.sleep(sleep_seconds)


def run_research_only(config_path: Path) -> None:
    """运行研究链并发布组合 release，不直接触发执行桥。"""
    config = load_config(config_path)
    shadow = json.loads(json.dumps(config, ensure_ascii=False))
    exec_cfg = dict(shadow.get('execution_bridge', {}) or {})
    exec_cfg['enabled'] = False
    shadow['execution_bridge'] = exec_cfg
    temp_path = Path(config_path).with_name(f"{Path(config_path).stem}.research_only.runtime.json")
    temp_path.write_text(json.dumps(shadow, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        run_integrated_supervisor(
            temp_path,
            run_mode_label='research_only',
            release_source_mode='research_only',
        )
    finally:
        try:
            temp_path.unlink()
        except Exception:
            pass


def run_release_only(
    config_path: Path,
    source_mode: str = "release_only",
    summary_path: str = "",
    target_positions_path: str = "",
    note: str = "",
    forced_trade_date: str = "",
) -> Dict[str, Any]:
    """仅把当前最新组合建议发布为可执行 release。"""
    config = load_config(config_path)
    release = publish_portfolio_release(
        config=config,
        source_mode=str(source_mode or "release_only"),
        profile=str(config.get('runtime_selection', {}).get('profile', '') or ''),
        summary_path=str(summary_path or ""),
        target_positions_path=str(target_positions_path or ""),
        note=str(note or ""),
        forced_trade_date=str(forced_trade_date or ""),
    )
    log_line(
        config,
        (
            "Supervisor: release_only 完成 "
            f"release_id={release.get('release_id', '')} "
            f"trade_date={release.get('trade_date', '')}"
        ),
    )
    return release
