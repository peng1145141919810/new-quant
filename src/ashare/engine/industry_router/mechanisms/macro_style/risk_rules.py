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

    regime_gap = abs(safe_float(signal_row.get('macro_regime_score'), 0.0) - safe_float(signal_row.get('style_flow_score'), 0.0))
    pre_10d = safe_float(signal_row.get('pre_10d_return'), 0.0)
    basket_fit = safe_float(signal_row.get('basket_fit_score'), 0.0)
    risk_floor = safe_float(signal_row.get('source_state_score'), 0.0)
    basket_strength = safe_float(signal_row.get('basket_relative_strength'), 0.0)

    if regime_gap >= thresholds.get('regime_gap', 0.35):
        penalty += 0.10
        penalty_flags.append('regime_instability_penalty')
    if pre_10d >= thresholds.get('crowding_runup', 0.10) and basket_fit >= 0.65:
        penalty += 0.08
        penalty_flags.append('style_crowding_penalty')
    if risk_floor <= thresholds.get('risk_off_floor', -0.10):
        penalty += 0.10
        penalty_flags.append('risk_off_shock_penalty')
    if basket_strength >= thresholds.get('basket_confirm', 0.015) and safe_float(signal_row.get('macro_regime_score'), 0.0) > 0:
        confirmation_bonus += 0.06
        confirmation_flags.append('basket_confirmation_score')
    if safe_float(signal_row.get('macro_regime_score'), 0.0) <= tuning.negative_state_exit:
        veto_triggered = True
        veto_reason = 'risk_off_shock_penalty'

    penalty = clip(penalty, 0.0, 1.0)
    confirmation_bonus = clip(confirmation_bonus, 0.0, 0.3)
    allow_entry = not veto_triggered and safe_float(signal_row.get('state_score'), 0.0) > tuning.negative_state_exit and safe_float(signal_row.get('basket_fit_score'), 0.0) >= 0.18
    return {
        'penalty': round(penalty, 4),
        'confirmation_bonus': round(confirmation_bonus, 4),
        'allow_entry': allow_entry,
        'flags': penalty_flags,
        'confirmation_flags': confirmation_flags,
        'veto_triggered': veto_triggered,
        'veto_reason': veto_reason,
        'penalty_detail': {
            'regime_instability_penalty': round(regime_gap, 4),
            'style_crowding_penalty': round(pre_10d, 4),
            'risk_off_shock_penalty': round(risk_floor, 4),
        },
        'confirmation_detail': {
            'basket_confirmation_score': round(basket_strength, 4),
        },
    }
