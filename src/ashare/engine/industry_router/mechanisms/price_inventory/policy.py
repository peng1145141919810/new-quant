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
    mechanism_group='price_inventory',
    router_keywords=('锂', '稀土', '铜', '库存', '涨价', '降价', '供需', '化工', 'mdi', '资源', '钴', '镍', '去库', '累库', '仓单', '价格'),
    event_state_weight=0.56,
    source_state_weight=0.44,
    mechanism_support_weight=0.18,
    signal_weight_event_state=0.48,
    signal_weight_source_state=0.40,
    signal_weight_event=0.58,
    signal_weight_mapping=0.24,
    signal_weight_profile=0.18,
    signal_weight_heat=0.20,
    entry_score=0.17,
    hold_score=0.08,
    exit_score=-0.08,
    negative_state_exit=-0.40,
    max_horizon_days=2,
    min_mapping_score=0.10,
    min_heat_score=0.10,
    low_mapping_penalty=0.08,
    low_heat_penalty=0.05,
    negative_state_penalty=0.20,
    source_conflict_penalty=0.10,
    low_liquidity_extra_penalty=0.05,
)


def entry_rule(signal_row, context, tuning, config) -> bool:
    return bool(signal_row.get('allow_entry')) and safe_float(signal_row.get('final_score')) >= tuning.entry_score and safe_float(signal_row.get('price_state_score')) > 0


def hold_rule(position_state, context, tuning, config) -> bool:
    max_days = int(position_state.get('horizon_days') or tuning.max_horizon_days)
    return safe_float(position_state.get('final_score')) >= tuning.hold_score and safe_float(position_state.get('state_score')) > tuning.negative_state_exit and int(position_state.get('days_held', 0)) < max_days


def exit_rule(position_state, context, tuning, config) -> bool:
    max_days = int(position_state.get('horizon_days') or tuning.max_horizon_days)
    return bool(position_state.get('veto_triggered')) or safe_float(position_state.get('inventory_state_score')) <= -0.15 or safe_float(position_state.get('final_score')) <= tuning.exit_score or int(position_state.get('days_held', 0)) >= max_days


def attribution_bucket(signal_or_position, tuning, config) -> str:
    commodity = safe_text(signal_or_position.get('commodity_primary'))
    if commodity:
        return commodity
    elasticity = safe_text(signal_or_position.get('elasticity_bucket'))
    return elasticity or safe_text(signal_or_position.get('subchain_primary')) or 'price_inventory'


def attribution_label(signal_or_position, tuning, config) -> str:
    parts = [
        safe_text(signal_or_position.get('commodity_primary')),
        safe_text(signal_or_position.get('elasticity_bucket')),
        safe_text(signal_or_position.get('direct_resource_link')),
    ]
    return ' | '.join([item for item in parts if item]) or 'price_inventory'


POLICY = BaseMechanismPolicy(
    name='price_inventory',
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
