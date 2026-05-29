# -*- coding: utf-8 -*-
"""策略家族升降级。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd

from hub.io_utils import write_csv, write_json
from hub.registry import load_registry


def evolve_strategy_family(registry_path: Path, family_output_dir: Path, evolution_rules: Dict[str, Any]) -> Dict[str, Any]:
    """刷新策略家族状态。

    Args:
        registry_path: 注册表路径。
        family_output_dir: 输出目录。
        evolution_rules: 升降级规则。

    Returns:
        状态摘要。
    """
    df = load_registry(registry_path)
    family_output_dir.mkdir(parents=True, exist_ok=True)
    if df.empty:
        payload = {'state_path': str(family_output_dir / 'strategy_family_state.csv'), 'n_members': 0}
        write_csv(family_output_dir / 'strategy_family_state.csv', pd.DataFrame())
        write_json(family_output_dir / 'promotion_actions.json', {'actions': []})
        return payload

    grp = df.groupby('strategy_key', dropna=False).agg(
        strategy_name=('strategy_name', 'last'),
        research_route=('research_route', 'last'),
        model_family=('model_family', 'last'),
        feature_profile=('feature_profile', 'last'),
        latest_total_score=('total_score', 'last'),
        latest_sharpe=('sharpe', 'last'),
        latest_max_drawdown=('max_drawdown', 'last'),
        mean_total_score=('total_score', 'mean'),
        score_std=('total_score', 'std'),
        count_runs=('run_id', 'count'),
    ).reset_index()
    grp['score_std'] = grp['score_std'].fillna(0.0)
    grp['stability_score'] = grp['mean_total_score'] - grp['score_std'] * float(evolution_rules.get('stability_std_penalty', 0.25))
    grp = grp.sort_values(['stability_score', 'latest_total_score'], ascending=[False, False]).reset_index(drop=True)

    champion_n = int(evolution_rules.get('champion_keep_top_n', 1) or 1)
    challenger_n = int(evolution_rules.get('challenger_keep_top_n', 3) or 3)
    min_runs_champion = int(evolution_rules.get('champion_min_runs', 2) or 2)
    champ_score = float(evolution_rules.get('champion_min_total_score', 45.0))
    chall_score = float(evolution_rules.get('challenger_min_total_score', 30.0))
    champ_sharpe = float(evolution_rules.get('champion_min_sharpe', 1.0))
    chall_sharpe = float(evolution_rules.get('challenger_min_sharpe', 0.5))
    champ_dd = float(evolution_rules.get('champion_max_drawdown_abs', 0.22))
    chall_dd = float(evolution_rules.get('challenger_max_drawdown_abs', 0.30))
    retire_score = float(evolution_rules.get('retire_below_total_score', 10.0))

    roles = []
    for idx, row in grp.iterrows():
        role = 'sandbox'
        action = 'keep'
        if idx < champion_n and row['count_runs'] >= min_runs_champion and row['latest_total_score'] >= champ_score and row['latest_sharpe'] >= champ_sharpe and abs(min(float(row['latest_max_drawdown']), 0.0)) <= champ_dd:
            role = 'champion'
            action = 'promote'
        elif idx < champion_n + challenger_n and row['latest_total_score'] >= chall_score and row['latest_sharpe'] >= chall_sharpe and abs(min(float(row['latest_max_drawdown']), 0.0)) <= chall_dd:
            role = 'challenger'
            action = 'promote'
        elif row['latest_total_score'] < retire_score:
            role = 'retired'
            action = 'retire'
        roles.append({'current_role': role, 'transition_action': action})
    role_df = pd.DataFrame(roles)
    state = pd.concat([grp, role_df], axis=1)
    write_csv(family_output_dir / 'strategy_family_state.csv', state)
    write_json(family_output_dir / 'promotion_actions.json', {'actions': state[['strategy_key', 'strategy_name', 'current_role', 'transition_action']].to_dict(orient='records')})
    return {'state_path': str(family_output_dir / 'strategy_family_state.csv'), 'n_members': int(len(state))}
