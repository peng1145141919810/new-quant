from __future__ import annotations

from typing import Any, Dict, List

from ...core.common import clip, safe_float, safe_text


def risk_filter(signal_row: Dict[str, Any], context: Dict[str, Any], tuning, config) -> Dict[str, Any]:
    thresholds = dict(config.get('risk_thresholds', {}) or {})
    penalty = 0.0
    confirmation_bonus = 0.0
    penalty_flags: List[str] = []
    confirmation_flags: List[str] = []
    veto_triggered = False
    veto_reason = ''

    pre_10d = safe_float(signal_row.get('pre_10d_return'), 0.0)
    price_state_score = safe_float(signal_row.get('price_state_score'), 0.0)
    inventory_state_score = safe_float(signal_row.get('inventory_state_score'), 0.0)
    source_state_score = safe_float(signal_row.get('source_state_score'), 0.0)
    volume_ratio = max(safe_float(signal_row.get('volume_ratio'), 1.0), safe_float(signal_row.get('amount_ratio_5d'), 1.0))
    elasticity_bucket = safe_text(signal_row.get('elasticity_bucket'))

    if pre_10d >= thresholds.get('overextension_start', 0.10):
        penalty += min(pre_10d, 0.18)
        penalty_flags.append('price_overextension_penalty')
    if inventory_state_score <= thresholds.get('inventory_reversal_floor', -0.12):
        penalty += 0.10
        penalty_flags.append('inventory_reversal_penalty')
    if price_state_score > 0 and source_state_score < 0:
        penalty += 0.08
        penalty_flags.append('supply_shock_fade_penalty')
    if elasticity_bucket == 'extreme' and pre_10d >= thresholds.get('elasticity_overheat', 0.09):
        penalty += 0.08
        penalty_flags.append('elasticity_overheat_penalty')
    if pre_10d >= thresholds.get('overextension_veto', 0.18) and inventory_state_score < 0:
        veto_triggered = True
        veto_reason = 'price_overextension_penalty'
    if price_state_score > 0 and inventory_state_score > 0 and volume_ratio >= thresholds.get('commodity_confirm', 1.08):
        confirmation_bonus += 0.06
        confirmation_flags.append('commodity_confirmation_score')

    penalty = clip(penalty, 0.0, 1.0)
    confirmation_bonus = clip(confirmation_bonus, 0.0, 0.3)
    allow_entry = not veto_triggered and safe_float(signal_row.get('state_score'), 0.0) > tuning.negative_state_exit and safe_float(signal_row.get('exposure_score'), 0.0) >= 0.18
    return {
        'penalty': round(penalty, 4),
        'confirmation_bonus': round(confirmation_bonus, 4),
        'allow_entry': allow_entry,
        'flags': penalty_flags,
        'confirmation_flags': confirmation_flags,
        'veto_triggered': veto_triggered,
        'veto_reason': veto_reason,
        'penalty_detail': {
            'price_overextension_penalty': round(pre_10d, 4),
            'inventory_reversal_penalty': round(inventory_state_score, 4),
            'supply_shock_fade_penalty': round(source_state_score, 4),
            'elasticity_overheat_penalty': elasticity_bucket,
        },
        'confirmation_detail': {
            'commodity_confirmation_score': round(volume_ratio, 4),
        },
    }
