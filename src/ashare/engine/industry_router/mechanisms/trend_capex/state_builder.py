from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

import pandas as pd

from ...contracts import MECHANISM_STATE_FIELDS
from ...core.common import classify_regime, clip, mean_or_zero, resolve_industry_factor_payload, safe_float, safe_text, signed_direction


def _event_group_score(group: pd.DataFrame) -> float:
    if group.empty:
        return 0.0
    signed = group.apply(lambda row: signed_direction(row['direction']) * safe_float(row['strength']) * safe_float(row['confidence']), axis=1)
    return round(mean_or_zero(signed.tolist()), 4)


def _build_rows(scope_type: str, scope_key: str, industry_primary: str, date_text: str, event_group: pd.DataFrame, source_row: Dict[str, Any], weights: Dict[str, float]) -> Dict[str, Any]:
    event_state = _event_group_score(event_group)
    demand_verification = clip(0.62 * event_state + 0.38 * safe_float(source_row.get('hit_balance_score'), 0.0), -1.0, 1.0)
    industry_expansion = clip(0.72 * safe_float(source_row.get('industry_expansion_score'), 0.0) + 0.28 * event_state, -1.0, 1.0)
    external_demand = clip(safe_float(source_row.get('external_demand_score'), 0.0), -1.0, 1.0)
    source_consensus_score = clip(safe_float(source_row.get('source_consensus_score'), 0.0), -1.0, 1.0)
    state_score = clip(
        weights.get('industry_expansion', 0.34) * industry_expansion
        + weights.get('demand_verification', 0.28) * demand_verification
        + weights.get('external_demand', 0.20) * external_demand
        + weights.get('source_consensus', 0.18) * source_consensus_score,
        -1.0,
        1.0,
    )
    counter = Counter(event_group['event_type'].astype(str).tolist()) if not event_group.empty else Counter()
    evidence_count = int(len(event_group) + safe_float(source_row.get('source_count'), 0))
    confidence = clip(0.55 * safe_float(source_row.get('confidence'), 0.0) + 0.45 * min(1.0, abs(event_state) + 0.08 * min(len(event_group), 4)), 0.0, 1.0)
    heat_score = clip(max(abs(event_state), safe_float(source_row.get('heat_score'), 0.0)), 0.0, 1.0)
    row = {
        'date': date_text,
        'mechanism_group': 'trend_capex',
        'scope_type': scope_type,
        'scope_key': scope_key,
        'industry_primary': industry_primary,
        'state_score': round(state_score, 4),
        'sub_state_1_name': 'industry_expansion',
        'sub_state_1': round(industry_expansion, 4),
        'sub_state_2_name': 'demand_verification',
        'sub_state_2': round(demand_verification, 4),
        'sub_state_3_name': 'external_demand',
        'sub_state_3': round(external_demand, 4),
        'confidence': round(confidence, 4),
        'source_consensus': round(source_consensus_score, 4),
        'event_state_score': round(event_state, 4),
        'source_state_score': round(safe_float(source_row.get('source_state_score'), 0.0), 4),
        'heat_score': round(heat_score, 4),
        'evidence_count': evidence_count,
        'key_driver_1': safe_text(counter.most_common(1)[0][0]) if counter else safe_text(source_row.get('key_driver_1')),
        'key_driver_2': safe_text(counter.most_common(2)[1][0]) if len(counter) > 1 else safe_text(source_row.get('key_driver_2')),
        'regime_label': classify_regime(state_score, heat_score),
        'industry_expansion_score': round(industry_expansion, 4),
        'demand_verification_score': round(demand_verification, 4),
        'external_demand_score': round(external_demand, 4),
        'source_consensus_score': round(source_consensus_score, 4),
        'notes': (
            f'trend_capex scope={scope_type} events={len(event_group)} '
            f'source_count={safe_float(source_row.get("source_count"), 0)} '
            f'sql_industry_match={"|".join(list(source_row.get("matched_industry_keys", []) or []))}'
        ).strip(),
    }
    for field in MECHANISM_STATE_FIELDS:
        row.setdefault(field, '' if field.endswith('_name') or field in {'scope_key', 'industry_primary', 'key_driver_1', 'key_driver_2', 'regime_label', 'notes'} else 0.0)
    return row


def build_state(raw_inputs: Dict[str, Any], source_state: pd.DataFrame, context: Dict[str, Any], tuning, config) -> pd.DataFrame:
    event_instances = raw_inputs.get('event_instances_df', pd.DataFrame())
    required_cols = {'mechanism_group', 'date', 'affected_industry', 'event_type', 'direction', 'strength', 'confidence'}
    if event_instances.empty or not required_cols.issubset(set(event_instances.columns)):
        event_subset = pd.DataFrame(columns=sorted(required_cols))
    else:
        event_subset = event_instances.loc[event_instances['mechanism_group'] == tuning.mechanism_group].copy()
    source_context = dict(context.get('source_context', {}) or {})
    source_by_date = dict(source_context.get('by_date', {}) or {})
    sql_context = dict(context.get('sql_factor_context', {}) or {})
    sql_by_date = dict(sql_context.get('by_date', {}) or {})
    dates = sorted(set(event_subset['date'].astype(str).tolist()) | set(source_by_date.keys()) | set(sql_by_date.keys()) | {safe_text(context.get('as_of_date'))})
    rows: List[Dict[str, Any]] = []
    weights = dict(config.get('state_weights', {}) or {})
    for date_text in [item for item in dates if item]:
        source_row = dict(source_by_date.get(date_text, {}) or {})
        sql_row = dict(sql_by_date.get(date_text, {}) or {})
        date_events = event_subset.loc[event_subset['date'].astype(str) == date_text].copy() if not event_subset.empty else event_subset.copy()
        for industry_primary, group in date_events.groupby('affected_industry', dropna=False):
            industry_key = safe_text(industry_primary)
            blended = dict(source_row)
            blended.update(dict(sql_row.get('overall', {}) or {}))
            matched_payload = resolve_industry_factor_payload(dict(sql_row.get('by_industry', {}) or {}), industry_key)
            blended.update(matched_payload)
            rows.append(_build_rows('industry', industry_key or tuning.mechanism_group, industry_key, date_text, group.copy(), blended, weights))
        mechanism_row = dict(source_row)
        mechanism_row.update(dict(sql_row.get('overall', {}) or {}))
        rows.append(_build_rows('mechanism', tuning.mechanism_group, '', date_text, date_events, mechanism_row, weights))
    frame = pd.DataFrame(rows)
    for field in MECHANISM_STATE_FIELDS:
        if field not in frame.columns:
            frame[field] = 0.0
    return frame[MECHANISM_STATE_FIELDS].sort_values(['date', 'scope_type', 'scope_key']).reset_index(drop=True)
