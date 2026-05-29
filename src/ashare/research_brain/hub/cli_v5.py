# -*- coding: utf-8 -*-
"""V5 主控入口。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from hub.candidate_factory import build_cycle_plan
from hub.config_utils import ensure_required_keys, load_config
from hub.data_scout import scout_data_sources
from hub.io_utils import ensure_dir, write_json
from hub.llm_client import LLMClient
from hub.logging_utils import setup_logger
from hub.research_diagnosis import diagnose_research_state
from hub.registry import load_registry
from hub.strategy_family import evolve_strategy_family
from hub.validate import validate_environment


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Args:
        None

    Returns:
        argparse.Namespace
    """
    p = argparse.ArgumentParser(description='量化研究中枢 V5')
    p.add_argument('--config', required=True)
    p.add_argument('--mode', default='adaptive_research_brain', choices=['validate_only', 'plan', 'batch', 'adaptive_research_brain'])
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--max-cycles', type=int, default=None)
    p.add_argument('--sleep-seconds', type=int, default=None)
    return p.parse_args()


def _logger(config: Dict[str, Any], name: str):
    """取日志器。

    Args:
        config: 配置。
        name: 名称。

    Returns:
        logger
    """
    return setup_logger(Path(str(config['hub_output_root'])) / 'logs', name)


def _refresh_family_and_gate(config: Dict[str, Any]) -> Dict[str, Any]:
    """刷新策略家族与部署闸门。

    Args:
        config: 配置。

    Returns:
        家族摘要。
    """
    hub_root = Path(str(config['hub_output_root']))
    registry_path = hub_root / 'registry' / 'experiment_registry.csv'
    family = evolve_strategy_family(registry_path=registry_path, family_output_dir=hub_root / 'strategy_family', evolution_rules=config.get('evolution_rules', {}))
    gate = {'deployment_ready': False, 'reason': '暂无 champion'}
    state_path = hub_root / 'strategy_family' / 'strategy_family_state.csv'
    if state_path.exists():
        import pandas as pd
        df = pd.read_csv(state_path)
        champ = df.loc[df['current_role'] == 'champion'].copy() if not df.empty else df
        if not champ.empty:
            champ = champ.sort_values(['stability_score', 'latest_total_score'], ascending=[False, False])
            row = champ.iloc[0]
            rule = config.get('deployment_gate', {})
            ready = float(row.get('latest_total_score', 0.0)) >= float(rule.get('min_total_score', 45.0)) and float(row.get('latest_sharpe', 0.0)) >= float(rule.get('min_sharpe', 1.0)) and abs(min(float(row.get('latest_max_drawdown', 0.0)), 0.0)) <= float(rule.get('max_drawdown_abs', 0.22))
            gate = {
                'deployment_ready': bool(ready),
                'candidate_strategy_key': str(row.get('strategy_key', '')),
                'candidate_strategy_name': str(row.get('strategy_name', '')),
                'latest_total_score': float(row.get('latest_total_score', 0.0)),
                'latest_sharpe': float(row.get('latest_sharpe', 0.0)),
                'latest_max_drawdown': float(row.get('latest_max_drawdown', 0.0)),
                'reason': '满足部署闸门' if ready else 'champion 尚未满足部署闸门',
            }
    write_json(hub_root / 'strategy_family' / 'deployment_gate.json', gate)
    return {'family': family, 'gate': gate}


def run_validate_only(config: Dict[str, Any]) -> Dict[str, Any]:
    """只做环境校验。

    Args:
        config: 配置。

    Returns:
        校验结果。
    """
    payload = validate_environment(config)
    write_json(Path(str(config['hub_output_root'])) / 'validation.json', payload)
    return payload


def _prepare_cycle(config: Dict[str, Any], cycle_index: int) -> Dict[str, Any]:
    """准备本轮。

    Args:
        config: 配置。
        cycle_index: 轮次。

    Returns:
        轮次上下文。
    """
    hub_root = Path(str(config['hub_output_root']))
    registry_path = hub_root / 'registry' / 'experiment_registry.csv'
    registry_df = load_registry(registry_path)
    cycle_id = f"cycle_{cycle_index:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    cycle_dir = ensure_dir(hub_root / 'cycles' / cycle_id)
    diagnosis = diagnose_research_state(registry_path=registry_path, output_path=cycle_dir / 'diagnosis.json')
    llm_client = LLMClient(config.get('llm_brain', {}))
    scout_result = scout_data_sources(config, cycle_dir=cycle_dir)
    plan = build_cycle_plan(base_config=config, registry_df=registry_df, cycle_index=cycle_index, diagnosis=diagnosis, llm_client=llm_client, cycle_dir=cycle_dir)
    return {'hub_root': hub_root, 'registry_path': registry_path, 'registry_df': registry_df, 'cycle_dir': cycle_dir, 'cycle_id': cycle_id, 'diagnosis': diagnosis, 'scout_result': scout_result, 'llm_client': llm_client, 'plan': plan}


def _load_resume_state(hub_root: Path) -> Dict[str, Any]:
    controller_path = hub_root / 'controller_state.json'
    if not controller_path.exists():
        return {'next_cycle': 1, 'history': [], 'resume': False}
    try:
        import json
        payload = json.loads(controller_path.read_text(encoding='utf-8'))
    except Exception:
        return {'next_cycle': 1, 'history': [], 'resume': False}
    history = list(payload.get('history', []) or [])
    stop_reason = str(payload.get('stop_reason', '') or '').strip()
    current_cycle_index = int(payload.get('current_cycle_index', 0) or 0)
    if not history or stop_reason:
        return {'next_cycle': 1, 'history': [], 'resume': False}
    return {
        'next_cycle': max(current_cycle_index + 1, 1),
        'history': history,
        'resume': True,
        'last_cycle_id': str(payload.get('last_cycle_id', '') or ''),
    }


def run_plan_only(config: Dict[str, Any]) -> Dict[str, Any]:
    """只生成计划。

    Args:
        config: 配置。

    Returns:
        计划摘要。
    """
    ctx = _prepare_cycle(config, cycle_index=1)
    return {
        'cycle_id': ctx['plan']['cycle_id'],
        'cycle_dir': str(ctx['plan']['cycle_dir']),
        'n_candidates': len(ctx['plan']['candidate_configs']),
        'budget': ctx['plan']['budget'],
        'diagnosis': ctx['diagnosis'],
    }


def run_batch(config: Dict[str, Any], dry_run: bool, cycle_index: int) -> Dict[str, Any]:
    """执行一轮。

    Args:
        config: 配置。
        dry_run: 是否空跑。
        cycle_index: 轮次。

    Returns:
        轮次摘要。
    """
    log = _logger(config, f'brain_cycle_{cycle_index:03d}')
    ctx = _prepare_cycle(config, cycle_index=cycle_index)
    plan = ctx['plan']
    results = []
    pyexe = str(config.get('execution', {}).get('python_executable', sys.executable) or sys.executable)
    candidate_runner = Path(__file__).resolve().parent.parent / 'run_single_candidate_v5.py'
    retryable_codes = {3221225477, 3221226356}
    log.info('本轮计划已生成。cycle=%s n_candidates=%s budget=%s', plan['cycle_id'], len(plan['candidate_configs']), plan['budget'])
    for idx, cfg in enumerate(plan['candidate_configs'], start=1):
        c = cfg['candidate']
        log.info('开始执行候选实验 %s/%s: %s | route=%s model=%s feature=%s logic=%s', idx, len(plan['candidate_configs']), c['strategy_name'], c['research_route'], c['model_family'], c['feature_profile'], c['training_logic'])
        result_path = Path(str(c['config_path'])).with_suffix('.result.json')
        if result_path.exists():
            result_path.unlink()
        proc = None
        for attempt in range(3):
            proc = subprocess.run(
                [pyexe, str(candidate_runner), '--config', str(c['config_path']), '--result-path', str(result_path), '--dry-run' if dry_run else '--no-dry-run'],
                cwd=str(candidate_runner.parent),
                check=False,
            )
            if proc.returncode == 0:
                break
            if proc.returncode not in retryable_codes or attempt >= 2:
                raise RuntimeError(f'candidate_subprocess_failed idx={idx} strategy_key={c["strategy_key"]} returncode={proc.returncode}')
            log.warning('candidate native crash detected; retrying idx=%s strategy_key=%s attempt=%s returncode=%s', idx, c['strategy_key'], attempt + 2, proc.returncode)
            if result_path.exists():
                result_path.unlink()
            time.sleep(5)
        ret = json.loads(result_path.read_text(encoding='utf-8'))
        results.append(ret['record'])
    family_gate = _refresh_family_and_gate(config)
    summary = {
        'cycle_id': plan['cycle_id'],
        'cycle_index': cycle_index,
        'n_candidates': len(plan['candidate_configs']),
        'budget': plan['budget'],
        'diagnosis': ctx['diagnosis'],
        'results': results,
        'family': family_gate['family'],
        'gate': family_gate['gate'],
        'stop_reason': '',
    }
    write_json(ctx['cycle_dir'] / 'cycle_summary.json', summary)
    return summary


def run_adaptive_research_brain(config: Dict[str, Any], dry_run: bool, max_cycles: Optional[int], sleep_seconds: Optional[int]) -> Dict[str, Any]:
    """持续研究脑闭环。

    Args:
        config: 配置。
        dry_run: 是否空跑。
        max_cycles: 最多轮次。
        sleep_seconds: 每轮间隔秒数。

    Returns:
        总结字典。
    """
    log = _logger(config, 'adaptive_research_brain')
    hub_root = Path(str(config['hub_output_root']))
    ensure_dir(hub_root)

    brain_cfg = dict(config.get('research_brain', {}))
    if max_cycles is None:
        max_cycles = brain_cfg.get('max_cycles')
    if sleep_seconds is None:
        sleep_seconds = int(brain_cfg.get('sleep_seconds', 0) or 0)

    resume_state = _load_resume_state(hub_root)
    cycle = int(resume_state.get('next_cycle', 1) or 1)
    history: List[Dict[str, Any]] = list(resume_state.get('history', []) or [])
    stop_reason = ''
    if bool(resume_state.get('resume', False)):
        log.info('检测到未完成 controller_state，断点续跑从 cycle=%s 开始。last_cycle_id=%s', cycle, str(resume_state.get('last_cycle_id', '') or ''))
    if max_cycles is not None and history and cycle > int(max_cycles):
        stop_reason = 'max_cycles_already_reached'
        final = {'n_cycles': len(history), 'history': history, 'stop_reason': stop_reason}
        write_json(hub_root / 'adaptive_loop_final.json', final)
        write_json(hub_root / 'controller_state.json', {
            'current_cycle_index': history[-1]['cycle_index'],
            'last_cycle_id': history[-1]['cycle_id'],
            'last_budget': history[-1]['budget'],
            'last_issues': history[-1]['issues'],
            'history': history,
            'stop_reason': stop_reason,
        })
        return final
    while True:
        summary = run_batch(config=config, dry_run=dry_run, cycle_index=cycle)
        top_score = max([float(r.get('total_score', 0.0) or 0.0) for r in summary.get('results', [])] + [0.0])
        history.append({
            'cycle_index': cycle,
            'cycle_id': summary['cycle_id'],
            'n_candidates': summary['n_candidates'],
            'top_score': top_score,
            'budget': summary['budget'],
            'issues': summary['diagnosis'].get('issues', []),
        })
        write_json(hub_root / 'controller_state.json', {
            'current_cycle_index': cycle,
            'last_cycle_id': summary['cycle_id'],
            'last_budget': summary['budget'],
            'last_issues': summary['diagnosis'].get('issues', []),
            'history': history,
            'stop_reason': '',
        })
        if max_cycles is not None and cycle >= int(max_cycles):
            stop_reason = 'max_cycles_reached'
            break
        cycle += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    final = {'n_cycles': len(history), 'history': history, 'stop_reason': stop_reason}
    write_json(hub_root / 'adaptive_loop_final.json', final)
    write_json(hub_root / 'controller_state.json', {
        'current_cycle_index': cycle,
        'last_cycle_id': history[-1]['cycle_id'] if history else '',
        'last_budget': history[-1]['budget'] if history else {},
        'last_issues': history[-1]['issues'] if history else [],
        'history': history,
        'stop_reason': stop_reason,
    })
    log.info('研究脑闭环结束。cycles=%s stop_reason=%s', len(history), stop_reason or 'manual_or_none')
    return final


def run_main(config: Dict[str, Any], mode: str, dry_run: bool, max_cycles: Optional[int], sleep_seconds: Optional[int]) -> Dict[str, Any]:
    """统一入口。

    Args:
        config: 配置。
        mode: 模式。
        dry_run: 是否空跑。
        max_cycles: 最大轮次。
        sleep_seconds: 间隔秒数。

    Returns:
        执行结果。
    """
    ensure_required_keys(config)
    if mode == 'validate_only':
        return run_validate_only(config)
    if mode == 'plan':
        return run_plan_only(config)
    if mode == 'batch':
        return run_batch(config, dry_run=dry_run, cycle_index=1)
    return run_adaptive_research_brain(config, dry_run=dry_run, max_cycles=max_cycles, sleep_seconds=sleep_seconds)


def run_local() -> Dict[str, Any]:
    """读取 local_settings 并运行。

    Args:
        None

    Returns:
        执行结果。
    """
    from hub import local_settings
    cfg = load_config(local_settings.CONFIG_PATH)
    return run_main(cfg, mode=local_settings.MODE, dry_run=bool(local_settings.DRY_RUN), max_cycles=local_settings.MAX_CYCLES, sleep_seconds=local_settings.SLEEP_SECONDS)


def main() -> None:
    """命令行主函数。

    Args:
        None

    Returns:
        None
    """
    args = parse_args()
    cfg = load_config(args.config)
    result = run_main(cfg, mode=args.mode, dry_run=bool(args.dry_run), max_cycles=args.max_cycles, sleep_seconds=args.sleep_seconds)
    print(result)


if __name__ == '__main__':
    main()
