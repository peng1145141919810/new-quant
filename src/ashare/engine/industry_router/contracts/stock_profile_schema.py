from __future__ import annotations

COMMON_STOCK_PROFILE_FIELDS = [
    'symbol', 'code', 'ts_code', 'name', 'industry_primary', 'industry_secondary', 'industry_bucket', 'mechanism_primary',
    'subchain_primary', 'core_driver_type', 'pricing_anchor', 'secondary_exposures', 'theme_primary', 'liquidity_bucket',
    'board', 'exchange', 'mapping_confidence', 'exposure_count', 'profile_score', 'notes',
]

TREND_CAPEX_PROFILE_FIELDS = [
    'customer_anchor', 'benefit_mode', 'spec_upgrade_level', 'global_vs_domestic_exposure',
]

PRICE_INVENTORY_PROFILE_FIELDS = [
    'resource_exposure', 'elasticity_bucket', 'cost_pass_through', 'direct_resource_link',
    'inventory_sensitivity', 'commodity_primary', 'downstream_pricing_power',
]

MACRO_STYLE_PROFILE_FIELDS = [
    'style_bucket', 'duration_sensitivity', 'yield_sensitivity', 'macro_beta_bucket',
    'credit_sensitivity', 'risk_appetite_sensitivity', 'defensive_vs_offensive',
]

STOCK_PROFILE_FIELDS = COMMON_STOCK_PROFILE_FIELDS + TREND_CAPEX_PROFILE_FIELDS + PRICE_INVENTORY_PROFILE_FIELDS + MACRO_STYLE_PROFILE_FIELDS
