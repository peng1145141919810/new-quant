from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from ...core.common import clip, normalize_symbol, safe_text


def map_event_to_stocks(event_row: Dict[str, Any], stock_profile_df: pd.DataFrame, context: Dict[str, Any], tuning, config) -> List[Dict[str, Any]]:
    subset = stock_profile_df.loc[stock_profile_df['mechanism_primary'] == tuning.mechanism_group].copy()
    if subset.empty:
        return []
    rows: List[Dict[str, Any]] = []
    weights = dict(config.get('mapping_weights', {}) or {})
    symbol_hint = normalize_symbol(event_row.get('symbol_hint'))
    subchain = safe_text(event_row.get('affected_subchain'))
    if symbol_hint and any(subset['symbol'].astype(str) == symbol_hint):
        rows.append(
            {
                'event_id': safe_text(event_row.get('event_id')),
                'date': safe_text(event_row.get('date')),
                'symbol': symbol_hint,
                'mapping_score': 1.0,
                'mapping_reason': 'security_code_direct',
                'exposure_level': 'direct',
                'is_core_beneficiary': True,
                'mechanism_primary': tuning.mechanism_group,
                'profile_hint': 'style_bucket',
                'mapping_weight_rule': 'security_code_direct',
            }
        )
        subset = subset.loc[subset['symbol'].astype(str) != symbol_hint].copy()
    spillover = safe_text(event_row.get('spillover_policy'))
    if spillover == 'none' or subset.empty:
        return rows
    for _, peer in subset.iterrows():
        score = 0.0
        reason = ''
        exposure_level = 'peer'
        if subchain and safe_text(peer.get('subchain_primary')) == subchain:
            score = weights.get('same_subchain_spillover', 0.28)
            reason = 'same_subchain_spillover'
            exposure_level = 'same_subchain'
        elif spillover in {'same_mechanism', 'limited', 'same_subchain'}:
            score = weights.get('same_mechanism_spillover', 0.14)
            reason = 'same_mechanism_spillover'
        if safe_text(peer.get('style_bucket')) in {'dividend', 'financial'}:
            score = min(1.0, score + 0.08)
        if score <= 0:
            continue
        rows.append(
            {
                'event_id': safe_text(event_row.get('event_id')),
                'date': safe_text(event_row.get('date')),
                'symbol': safe_text(peer.get('symbol')),
                'mapping_score': round(clip(score * max(float(peer.get('mapping_confidence') or 0.0), 0.4), 0.0, 1.0), 4),
                'mapping_reason': reason,
                'exposure_level': exposure_level,
                'is_core_beneficiary': False,
                'mechanism_primary': tuning.mechanism_group,
                'profile_hint': safe_text(peer.get('style_bucket')),
                'mapping_weight_rule': reason,
            }
        )
    return rows
