from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Tuple

import pandas as pd

from ..contracts import MECHANISM_GROUPS
from .common import (
    LIQUIDITY_RANK,
    clip,
    normalize_importance,
    normalize_symbol,
    parse_confidence,
    parse_date,
    safe_float,
    safe_int,
    safe_text,
    signed_direction,
)


def keyword_mechanism(text: str, policy_map: Mapping[str, Any]) -> str:
    raw = safe_text(text).lower()
    hits: Dict[str, int] = {}
    for mechanism, policy in policy_map.items():
        count = 0
        for token in tuple(getattr(policy, 'tuning').router_keywords):
            word = safe_text(token).lower()
            if word and word in raw:
                count += 1
        if count > 0:
            hits[mechanism] = count
    if not hits:
        return ''
    return sorted(hits.items(), key=lambda item: (-item[1], item[0]))[0][0]


def resolve_direction(raw_event: Dict[str, Any], taxonomy: Dict[str, Any]) -> str:
    direct = safe_text(raw_event.get('event_direction')).lower()
    if direct in {'positive', 'negative', 'neutral'}:
        return direct
    rule = safe_text(taxonomy.get('direction_default')).lower()
    facts = dict(raw_event.get('structured_facts', {}) or {})
    if rule == 'follow_report':
        profit_change = safe_float(facts.get('net_profit_change_percentage'), 0.0)
        if profit_change > 5.0:
            return 'positive'
        if profit_change < -5.0:
            return 'negative'
        return 'neutral'
    if rule == 'follow_price':
        title = safe_text(raw_event.get('raw_title') or raw_event.get('title')).lower()
        if any(token in title for token in ['涨价', '上调', '提价']):
            return 'positive'
        if any(token in title for token in ['跌价', '下调', '降价']):
            return 'negative'
        return 'neutral'
    if rule in {'positive', 'negative', 'neutral'}:
        return rule
    return 'neutral'


def resolve_mechanism(
    raw_event: Dict[str, Any],
    taxonomy: Dict[str, Any],
    stock_lookup: Dict[str, Dict[str, Any]],
    stock_by_name: Dict[str, Dict[str, Any]],
    policy_map: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any], str]:
    symbol = normalize_symbol(raw_event.get('security_code'))
    stock_row = stock_lookup.get(symbol)
    if stock_row is None:
        company_name = safe_text(raw_event.get('company_name'))
        stock_row = stock_by_name.get(company_name)
        if stock_row is not None:
            symbol = safe_text(stock_row.get('symbol'))
    stock_mechanism = safe_text(stock_row.get('mechanism_primary')) if stock_row is not None else ''
    explicit = safe_text(taxonomy.get('mechanism_group'))

    if explicit == 'inherit_stock_mechanism' and stock_mechanism in MECHANISM_GROUPS:
        return stock_mechanism, stock_row if stock_row is not None else {}, 'stock_master_inherit'
    if explicit in MECHANISM_GROUPS:
        return explicit, stock_row if stock_row is not None else {}, 'taxonomy_fixed'
    if explicit == 'router_required' and stock_mechanism in MECHANISM_GROUPS:
        return stock_mechanism, stock_row if stock_row is not None else {}, 'stock_master_router_fallback'
    if stock_mechanism in MECHANISM_GROUPS:
        return stock_mechanism, stock_row if stock_row is not None else {}, 'stock_master_direct'

    title_blob = ' '.join(
        [
            safe_text(raw_event.get('raw_title') or raw_event.get('title')),
            safe_text(raw_event.get('company_name')),
            safe_text(raw_event.get('summary')),
            safe_text(raw_event.get('source_name')),
        ]
    )
    keyword = keyword_mechanism(title_blob, policy_map=policy_map)
    if keyword:
        return keyword, {}, 'keyword_router'
    return '', {}, 'unmapped'


def event_strength(raw_event: Dict[str, Any], taxonomy: Dict[str, Any], direction: str) -> float:
    facts = dict(raw_event.get('structured_facts', {}) or {})
    importance = normalize_importance(raw_event.get('importance_score') or facts.get('importance_score'))
    rule_score = min(max(safe_float(facts.get('rule_score'), 0.0) / 3.0, 0.0), 1.0)
    quality = min(max(safe_float(facts.get('evidence_quality_score'), 0.55), 0.0), 1.0)
    weight = safe_float(taxonomy.get('event_weight'), 0.50)
    signed = abs(signed_direction(direction))
    strength = weight * (0.55 + 0.25 * importance + 0.20 * quality) + 0.12 * rule_score
    if signed == 0:
        strength *= 0.35
    return round(clip(strength, 0.0, 1.2), 4)


def build_event_instances(
    events: List[Dict[str, Any]],
    taxonomy_lookup: Dict[str, Dict[str, Any]],
    stock_profile_df: pd.DataFrame,
    policy_map: Mapping[str, Any],
) -> pd.DataFrame:
    if stock_profile_df.empty:
        return pd.DataFrame(columns=[
            'event_id', 'date', 'source_type', 'source_ref', 'event_type', 'mechanism_group', 'affected_industry',
            'affected_subchain', 'direction', 'strength', 'confidence', 'half_life_days', 'symbol_hint', 'company_name',
            'raw_title', 'spillover_policy', 'routing_reason',
        ])
    stock_lookup = {str(row['symbol']): row for _, row in stock_profile_df.iterrows()}
    stock_by_name = {str(row['name']): row for _, row in stock_profile_df.iterrows() if safe_text(row.get('name'))}
    rows: List[Dict[str, Any]] = []
    for raw in events:
        event_type = safe_text(raw.get('event_type')) or 'unknown'
        taxonomy = dict(taxonomy_lookup.get(event_type, {}))
        date_text = parse_date(raw.get('publish_time') or raw.get('crawl_time'))
        if not date_text:
            continue
        mechanism, stock_row, routing_reason = resolve_mechanism(
            raw_event=raw,
            taxonomy=taxonomy,
            stock_lookup=stock_lookup,
            stock_by_name=stock_by_name,
            policy_map=policy_map,
        )
        if mechanism not in MECHANISM_GROUPS:
            continue
        direction = resolve_direction(raw_event=raw, taxonomy=taxonomy)
        confidence = parse_confidence(raw.get('confidence'))
        strength = event_strength(raw_event=raw, taxonomy=taxonomy, direction=direction)
        rows.append(
            {
                'event_id': safe_text(raw.get('event_id')),
                'date': date_text,
                'source_type': safe_text(raw.get('source_type')) or 'unknown',
                'source_ref': safe_text(raw.get('source_name')) or safe_text(raw.get('event_id')),
                'event_type': event_type,
                'mechanism_group': mechanism,
                'affected_industry': safe_text(stock_row.get('industry_primary')),
                'affected_subchain': safe_text(stock_row.get('subchain_primary')),
                'direction': direction,
                'strength': strength,
                'confidence': round(confidence, 4),
                'half_life_days': safe_int(taxonomy.get('half_life_days'), 7),
                'symbol_hint': safe_text(stock_row.get('symbol')) or normalize_symbol(raw.get('security_code')),
                'company_name': safe_text(raw.get('company_name')),
                'raw_title': safe_text(raw.get('raw_title') or raw.get('title')),
                'spillover_policy': safe_text(taxonomy.get('spillover_policy')) or 'none',
                'routing_reason': routing_reason,
            }
        )
    return pd.DataFrame(rows)


def build_event_stock_mapping(event_instances_df: pd.DataFrame, stock_profile_df: pd.DataFrame, policy_map: Mapping[str, Any]) -> pd.DataFrame:
    columns = ['event_id', 'date', 'symbol', 'mapping_score', 'mapping_reason', 'exposure_level', 'is_core_beneficiary', 'mechanism_primary', 'profile_hint', 'mapping_weight_rule']
    if event_instances_df.empty or stock_profile_df.empty:
        return pd.DataFrame(columns=columns)
    rows: List[Dict[str, Any]] = []
    for _, event in event_instances_df.iterrows():
        mechanism = safe_text(event.get('mechanism_group'))
        policy = policy_map.get(mechanism)
        if policy is None:
            continue
        mapped_rows = list(
            policy.map_event_to_stocks(
                event_row=event.to_dict(),
                stock_profile_df=stock_profile_df,
                context={'event_instances_df': event_instances_df},
            )
            or []
        )
        rows.extend(mapped_rows)
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    for field in columns:
        if field not in df.columns:
            df[field] = ''
    return df.drop_duplicates(subset=['event_id', 'symbol', 'mapping_reason'], keep='first').reset_index(drop=True)


def summarize_event_drivers(event_instances_df: pd.DataFrame, mechanism: str, date_text: str) -> Tuple[str, str]:
    if event_instances_df.empty:
        return '', ''
    subset = event_instances_df.loc[
        (event_instances_df['mechanism_group'] == mechanism)
        & (event_instances_df['date'].astype(str) == safe_text(date_text))
    ]
    counter = Counter(subset['event_type'].astype(str).tolist())
    top = [name for name, _ in counter.most_common(2)]
    return (top[0] if len(top) > 0 else '', top[1] if len(top) > 1 else '')
