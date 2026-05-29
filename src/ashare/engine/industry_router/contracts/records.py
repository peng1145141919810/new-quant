from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

MECHANISM_GROUPS = ('trend_capex', 'price_inventory', 'macro_style')


@dataclass(frozen=True)
class StockProfileRecord:
    symbol: str
    code: str
    ts_code: str
    name: str
    industry_primary: str
    industry_secondary: str
    industry_bucket: str
    mechanism_primary: str
    subchain_primary: str
    core_driver_type: str
    pricing_anchor: str
    secondary_exposures: str
    theme_primary: str
    liquidity_bucket: str
    board: str
    exchange: str
    mapping_confidence: float
    exposure_count: int
    profile_score: float
    customer_anchor: str = ''
    benefit_mode: str = ''
    spec_upgrade_level: str = ''
    global_vs_domestic_exposure: str = ''
    resource_exposure: str = ''
    elasticity_bucket: str = ''
    cost_pass_through: str = ''
    direct_resource_link: str = ''
    inventory_sensitivity: str = ''
    commodity_primary: str = ''
    downstream_pricing_power: str = ''
    style_bucket: str = ''
    duration_sensitivity: str = ''
    yield_sensitivity: str = ''
    macro_beta_bucket: str = ''
    credit_sensitivity: str = ''
    risk_appetite_sensitivity: str = ''
    defensive_vs_offensive: str = ''
    notes: str = ''


@dataclass(frozen=True)
class EventInstanceRecord:
    event_id: str
    date: str
    source_type: str
    source_ref: str
    event_type: str
    mechanism_group: str
    affected_industry: str
    affected_subchain: str
    direction: str
    strength: float
    confidence: float
    half_life_days: int
    symbol_hint: str
    company_name: str
    raw_title: str
    spillover_policy: str
    routing_reason: str


@dataclass(frozen=True)
class EventStockMappingRecord:
    event_id: str
    date: str
    symbol: str
    mapping_score: float
    mapping_reason: str
    exposure_level: str
    is_core_beneficiary: bool
    mechanism_primary: str
    profile_hint: str = ''
    mapping_weight_rule: str = ''


@dataclass(frozen=True)
class SourceStateRecord:
    date: str
    mechanism_group: str
    source_id: str
    source_name: str
    category: str
    source_signal_score: float
    confidence: float
    publish_date: str
    title: str
    summary: str
    url: str
    source_weight: float
    category_weight: float
    freshness_weight: float
    positive_hits: str = ''
    negative_hits: str = ''


@dataclass(frozen=True)
class MechanismStateDailyRecord:
    date: str
    mechanism_group: str
    scope_type: str
    scope_key: str
    industry_primary: str
    state_score: float
    sub_state_1_name: str
    sub_state_1: float
    sub_state_2_name: str
    sub_state_2: float
    sub_state_3_name: str
    sub_state_3: float
    confidence: float
    source_consensus: float
    event_state_score: float
    source_state_score: float
    heat_score: float
    evidence_count: int
    key_driver_1: str
    key_driver_2: str
    regime_label: str
    industry_expansion_score: float = 0.0
    demand_verification_score: float = 0.0
    external_demand_score: float = 0.0
    source_consensus_score: float = 0.0
    price_momentum_score: float = 0.0
    inventory_tightness_score: float = 0.0
    supply_demand_balance_score: float = 0.0
    trade_flow_verification_score: float = 0.0
    macro_regime_score: float = 0.0
    style_rotation_score: float = 0.0
    risk_appetite_score: float = 0.0
    liquidity_condition_score: float = 0.0
    notes: str = ''


@dataclass(frozen=True)
class CoreVariableDailyRecord:
    symbol: str
    date: str
    mechanism_primary: str
    industry_primary: str
    subchain_primary: str
    base_score: float
    state_score: float
    industry_state_score: float
    mechanism_state_score: float
    event_state_score: float
    source_state_score: float
    event_score: float
    mapping_score: float
    profile_score: float
    heat_score: float
    exposure_score: float
    price_state_score: float
    inventory_state_score: float
    macro_regime_score: float
    style_flow_score: float
    basket_fit_score: float
    liquidity_bucket: str
    dominant_event_label: str = ''
    dominant_state_driver: str = ''
    dominant_source_driver: str = ''


@dataclass(frozen=True)
class StockSignalDailyRecord:
    symbol: str
    date: str
    mechanism_primary: str
    industry_primary: str
    subchain_primary: str
    base_score: float
    state_score: float
    industry_state_score: float
    mechanism_state_score: float
    event_state_score: float
    source_state_score: float
    event_score: float
    mapping_score: float
    profile_score: float
    heat_score: float
    exposure_score: float
    price_state_score: float
    inventory_state_score: float
    macro_regime_score: float
    style_flow_score: float
    basket_fit_score: float
    confirmation_score: float
    risk_penalty: float
    pre_risk_score: float
    final_score: float
    penalty_score: float
    confirmation_bonus: float
    veto_triggered: bool
    veto_reason: str
    signal_state: str
    allow_entry: bool
    attribution_bucket: str
    attribution_label: str
    reason_top: str
    confirmation_flags: str
    risk_flags: str
    penalty_detail_json: str = ''
    confirmation_detail_json: str = ''
    profile_context_json: str = ''


@dataclass(frozen=True)
class BacktestTradeRecord:
    mechanism_group: str
    signal_date: str
    symbol: str
    entry_date: str
    exit_date: str
    horizon_days: int
    days_held: int
    forward_return: float
    final_score: float
    signal_state: str
    attribution_bucket: str
    attribution_label: str
    reason_top: str


@dataclass(frozen=True)
class PolicyTuning:
    mechanism_group: str
    router_keywords: tuple[str, ...] = ()
    event_state_weight: float = 0.5
    source_state_weight: float = 0.5
    mechanism_support_weight: float = 0.2
    signal_weight_event_state: float = 0.3
    signal_weight_source_state: float = 0.3
    signal_weight_event: float = 0.3
    signal_weight_mapping: float = 0.2
    signal_weight_profile: float = 0.2
    signal_weight_heat: float = 0.1
    entry_score: float = 0.7
    hold_score: float = 0.3
    exit_score: float = 0.0
    negative_state_exit: float = -0.3
    max_horizon_days: int = 3
    min_mapping_score: float = 0.0
    min_heat_score: float = 0.0
    low_mapping_penalty: float = 0.0
    low_heat_penalty: float = 0.0
    negative_state_penalty: float = 0.0
    source_conflict_penalty: float = 0.0
    low_liquidity_extra_penalty: float = 0.0


def as_record_dict(record: Any) -> Dict[str, Any]:
    return asdict(record)
