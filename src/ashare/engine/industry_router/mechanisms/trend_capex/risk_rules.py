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
    pre_5d = safe_float(signal_row.get('pre_5d_return'), 0.0)
    volume_ratio = max(safe_float(signal_row.get('volume_ratio'), 1.0), safe_float(signal_row.get('amount_ratio_5d'), 1.0))
    rel_strength = safe_float(signal_row.get('basket_relative_strength'), 0.0)
    mapping_score = safe_float(signal_row.get('mapping_score'), 0.0)

    if safe_text(signal_row.get('benefit_mode')) == 'theme_only':
        penalty += thresholds.get('theme_only_penalty', 0.16)
        penalty_flags.append('concept_only_penalty')
    if pre_5d >= thresholds.get('runup_penalty_start', 0.12):
        penalty += min(pre_5d, thresholds.get('high_positioning_penalty', 0.10))
        penalty_flags.append('pre_event_runup')
    if pre_10d >= thresholds.get('runup_veto', 0.22) and mapping_score < 0.45:
        veto_triggered = True
        veto_reason = 'high_positioning_penalty'
    if rel_strength >= thresholds.get('relative_strength_confirm', 0.02):
        confirmation_bonus += 0.05
        confirmation_flags.append('post_event_rel_strength')
    if volume_ratio >= thresholds.get('volume_confirm', 1.15):
        confirmation_bonus += 0.04
        confirmation_flags.append('post_event_volume_ratio')
    if safe_float(signal_row.get('pct_chg'), 0.0) > 0 and rel_strength > 0:
        confirmation_bonus += 0.03
        confirmation_flags.append('post_event_hold_strength')
    if mapping_score < tuning.min_mapping_score:
        penalty += tuning.low_mapping_penalty
        penalty_flags.append('low_mapping')

    penalty = clip(penalty, 0.0, 1.0)
    confirmation_bonus = clip(confirmation_bonus, 0.0, 0.3)
    allow_entry = not veto_triggered and safe_float(signal_row.get('state_score'), 0.0) > tuning.negative_state_exit and mapping_score >= max(0.05, 0.75 * tuning.min_mapping_score)
    return {
        'penalty': round(penalty, 4),
        'confirmation_bonus': round(confirmation_bonus, 4),
        'allow_entry': allow_entry,
        'flags': penalty_flags,
        'confirmation_flags': confirmation_flags,
        'veto_triggered': veto_triggered,
        'veto_reason': veto_reason,
        'penalty_detail': {
            'pre_event_runup': round(pre_5d, 4),
            'concept_only_penalty': safe_text(signal_row.get('benefit_mode')) == 'theme_only',
            'high_positioning_penalty': round(pre_10d, 4),
        },
        'confirmation_detail': {
            'post_event_rel_strength': round(rel_strength, 4),
            'post_event_volume_ratio': round(volume_ratio, 4),
            'post_event_hold_strength': round(safe_float(signal_row.get('pct_chg'), 0.0), 4),
        },
    }
