from __future__ import annotations

import json
from pathlib import Path

from ...contracts import PolicyTuning
from ...core.common import safe_float, safe_text
from ..base import BaseMechanismPolicy
from .mapping_rules import map_event_to_stocks
from .profile_view import build_profile
from .risk_rules import risk_filter
from .signal_builder import build_core_variables, generate_signal
from .source_ingest import summarize_source_state
from .state_builder import build_state


def _load_config() -> dict:
    path = Path(__file__).with_name('config.json')
    return json.loads(path.read_text(encoding='utf-8-sig'))


CONFIG = _load_config()
TUNING = PolicyTuning(
    mechanism_group='trend_capex',
    router_keywords=('算力', '光模块', '芯片', '半导体', '服务器', '交换机', '数据中心', 'idc', 'hbm', '存储', '工业通信', '通信', '订单', '中标', 'capex', '投资'),
    event_state_weight=0.62,
    source_state_weight=0.38,
    mechanism_support_weight=0.22,
    signal_weight_event_state=0.55,
    signal_weight_source_state=0.32,
    signal_weight_event=0.62,
    signal_weight_mapping=0.28,
    signal_weight_profile=0.24,
    signal_weight_heat=0.18,
    entry_score=0.34,
    hold_score=0.14,
    exit_score=-0.06,
    negative_state_exit=-0.45,
    max_horizon_days=3,
    min_mapping_score=0.12,
    min_heat_score=0.08,
    low_mapping_penalty=0.10,
    low_heat_penalty=0.06,
    negative_state_penalty=0.22,
    source_conflict_penalty=0.08,
    low_liquidity_extra_penalty=0.04,
)


def entry_rule(signal_row, context, tuning, config) -> bool:
    return bool(signal_row.get('allow_entry')) and safe_float(signal_row.get('final_score')) >= tuning.entry_score and safe_float(signal_row.get('mapping_score')) >= tuning.min_mapping_score


def hold_rule(position_state, context, tuning, config) -> bool:
    max_days = int(position_state.get('horizon_days') or tuning.max_horizon_days)
    return safe_float(position_state.get('final_score')) >= tuning.hold_score and int(position_state.get('days_held', 0)) < max_days


def exit_rule(position_state, context, tuning, config) -> bool:
    max_days = int(position_state.get('horizon_days') or tuning.max_horizon_days)
    return bool(position_state.get('veto_triggered')) or safe_float(position_state.get('state_score')) <= tuning.negative_state_exit or safe_float(position_state.get('final_score')) <= tuning.exit_score or int(position_state.get('days_held', 0)) >= max_days


def attribution_bucket(signal_or_position, tuning, config) -> str:
    benefit_mode = safe_text(signal_or_position.get('benefit_mode'))
    if benefit_mode in {'direct_order', 'capacity_pull', 'spec_upgrade', 'theme_only'}:
        return benefit_mode
    return safe_text(signal_or_position.get('subchain_primary')) or 'trend_capex'


def attribution_label(signal_or_position, tuning, config) -> str:
    parts = [
        safe_text(signal_or_position.get('subchain_primary')),
        safe_text(signal_or_position.get('benefit_mode')),
        safe_text(signal_or_position.get('reason_top')),
    ]
    return ' | '.join([item for item in parts if item]) or 'trend_capex'


POLICY = BaseMechanismPolicy(
    name='trend_capex',
    tuning=TUNING,
    config=CONFIG,
    profile_builder=build_profile,
    source_summarizer=summarize_source_state,
    state_builder=build_state,
    mapping_builder=map_event_to_stocks,
    core_variable_builder=build_core_variables,
    signal_builder=generate_signal,
    risk_builder=risk_filter,
    entry_rule_fn=entry_rule,
    hold_rule_fn=hold_rule,
    exit_rule_fn=exit_rule,
    attribution_bucket_fn=attribution_bucket,
    attribution_label_fn=attribution_label,
)
