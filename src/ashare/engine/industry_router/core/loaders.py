from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

from ...config_utils import ensure_dir
from ..contracts import MECHANISM_GROUPS, STOCK_PROFILE_FIELDS
from .common import (
    BENEFIT_MODE_BUCKET,
    CUSTOMER_ANCHOR_BUCKET,
    DEFENSIVE_BUCKET,
    DIRECT_RESOURCE_BUCKET,
    ELASTICITY_BUCKET,
    GLOBAL_EXPOSURE_BUCKET,
    LOW_MID_HIGH,
    PASS_THROUGH_BUCKET,
    STYLE_BUCKET,
    liquidity_profile_score,
    map_bucket_score,
    normalize_symbol,
    parse_date,
    safe_float,
    safe_text,
    split_exposures,
    symbol_to_code,
)

CONTRACT_FILES = ['stock_master.seed.csv', 'mechanism_map.seed.csv', 'event_taxonomy.json', 'source_contracts.json']


def contract_root(config: Dict[str, Any]) -> Path:
    raw = safe_text(config.get('industry_router', {}).get('contract_root'))
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[3] / 'configs' / 'industry_router'


def output_root(config: Dict[str, Any]) -> Path:
    raw = safe_text(config.get('industry_router', {}).get('output_root'))
    if raw:
        return ensure_dir(Path(raw))
    return ensure_dir(Path(str(config['paths']['research_root'])) / 'industry_router')


def copy_contract_snapshot(contract_root_path: Path, output_root_path: Path) -> None:
    target = ensure_dir(output_root_path / 'contracts')
    for name in CONTRACT_FILES:
        src = contract_root_path / name
        if src.exists():
            shutil.copyfile(src, target / name)


def load_listing_lookup(config: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    listing_path = Path(str(config.get('market_pipeline', {}).get('listing_master_path', '') or '').strip())
    symbol_lookup: Dict[str, Dict[str, Any]] = {}
    code_lookup: Dict[str, Dict[str, Any]] = {}
    if not listing_path.exists():
        return symbol_lookup, code_lookup
    try:
        df = pd.read_csv(listing_path, encoding='utf-8-sig')
    except Exception:
        return symbol_lookup, code_lookup
    for _, row in df.iterrows():
        item = row.to_dict()
        symbol = normalize_symbol(item.get('ts_code') or item.get('code'))
        code = symbol_to_code(symbol)
        if symbol:
            symbol_lookup[symbol] = item
        if code:
            code_lookup[code] = item
    return symbol_lookup, code_lookup


def estimate_liquidity_bucket(enriched_dir: Path, symbol: str) -> str:
    code = symbol_to_code(symbol)
    if not code:
        return 'C'
    path = enriched_dir / f'{code}.csv'
    if not path.exists():
        return 'C'
    try:
        df = pd.read_csv(path, usecols=['amount'])
    except Exception:
        return 'C'
    if df.empty:
        return 'C'
    amounts = pd.to_numeric(df['amount'], errors='coerce').dropna().tail(20)
    if amounts.empty:
        return 'C'
    mean_amount = float(amounts.mean())
    if mean_amount >= 300000:
        return 'A'
    if mean_amount >= 100000:
        return 'B'
    return 'C'


def resolve_stock_master(config: Dict[str, Any], contract_root_path: Path) -> pd.DataFrame:
    seed_path = contract_root_path / 'stock_master.seed.csv'
    df = pd.read_csv(seed_path, encoding='utf-8-sig').fillna('')
    enriched_dir = Path(str(config.get('market_pipeline', {}).get('enriched_dir', '') or '').strip())
    symbol_lookup, code_lookup = load_listing_lookup(config)
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        item = row.to_dict()
        symbol = normalize_symbol(item.get('symbol'))
        code = symbol_to_code(symbol)
        listing = symbol_lookup.get(symbol) or code_lookup.get(code) or {}
        bucket = safe_text(item.get('liquidity_bucket')).upper() or estimate_liquidity_bucket(enriched_dir, symbol)
        rows.append(
            {
                'symbol': symbol,
                'code': code,
                'ts_code': symbol,
                'name': safe_text(item.get('name')) or safe_text(listing.get('name')),
                'industry_primary': safe_text(item.get('industry_primary')) or safe_text(listing.get('industry')),
                'industry_secondary': safe_text(item.get('industry_secondary')),
                'industry_bucket': safe_text(item.get('industry_bucket')),
                'mechanism_primary': safe_text(item.get('mechanism_primary')),
                'subchain_primary': safe_text(item.get('subchain_primary')),
                'secondary_exposures': '|'.join(split_exposures(item.get('secondary_exposures'))),
                'theme_primary': safe_text(item.get('theme_primary')),
                'liquidity_bucket': bucket or 'C',
                'board': safe_text(listing.get('board')),
                'exchange': safe_text(listing.get('exchange')),
                'notes': safe_text(item.get('notes')),
            }
        )
    return pd.DataFrame(rows)


def resolve_mechanism_map(contract_root_path: Path) -> pd.DataFrame:
    seed_path = contract_root_path / 'mechanism_map.seed.csv'
    df = pd.read_csv(seed_path, encoding='utf-8-sig').fillna('')
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        item = row.to_dict()
        normalized = {key: safe_text(value) for key, value in item.items()}
        normalized['symbol'] = normalize_symbol(item.get('symbol'))
        normalized['secondary_exposures'] = '|'.join(split_exposures(item.get('secondary_exposures')))
        normalized['mapping_confidence'] = round(safe_float(item.get('mapping_confidence'), 0.0), 4)
        rows.append(normalized)
    return pd.DataFrame(rows)


def build_stock_profile(stock_master_df: pd.DataFrame, mechanism_map_df: pd.DataFrame) -> pd.DataFrame:
    if stock_master_df.empty:
        return pd.DataFrame(columns=STOCK_PROFILE_FIELDS)
    merged = stock_master_df.merge(mechanism_map_df, on='symbol', how='left', suffixes=('', '_map'))
    rows: List[Dict[str, Any]] = []
    for _, row in merged.iterrows():
        exposures = split_exposures(row.get('secondary_exposures_map') or row.get('secondary_exposures'))
        mapping_confidence = safe_float(row.get('mapping_confidence'), 0.0)
        liquidity_score = liquidity_profile_score(row.get('liquidity_bucket'))
        benefit_score = map_bucket_score(row.get('benefit_mode'), BENEFIT_MODE_BUCKET, 0.45)
        resource_score = map_bucket_score(row.get('direct_resource_link'), DIRECT_RESOURCE_BUCKET, 0.45)
        style_score = map_bucket_score(row.get('style_bucket'), STYLE_BUCKET, 0.5)
        profile_score = min(
            1.0,
            round(
                0.30 * mapping_confidence
                + 0.18 * liquidity_score
                + 0.10 * benefit_score
                + 0.08 * resource_score
                + 0.08 * style_score
                + 0.08 * map_bucket_score(row.get('customer_anchor'), CUSTOMER_ANCHOR_BUCKET, 0.45)
                + 0.08 * map_bucket_score(row.get('global_vs_domestic_exposure'), GLOBAL_EXPOSURE_BUCKET, 0.45)
                + 0.05 * map_bucket_score(row.get('elasticity_bucket'), ELASTICITY_BUCKET, 0.45)
                + 0.05 * map_bucket_score(row.get('defensive_vs_offensive'), DEFENSIVE_BUCKET, 0.5)
                ,
                4,
            ),
        )
        mechanism_primary = safe_text(row.get('mechanism_primary_map') or row.get('mechanism_primary'))
        subchain_primary = safe_text(row.get('subchain_primary_map') or row.get('subchain_primary'))
        notes = ' | '.join([text for text in [safe_text(row.get('notes')), safe_text(row.get('notes_map'))] if text])
        payload = {
            'symbol': safe_text(row.get('symbol')),
            'code': safe_text(row.get('code')),
            'ts_code': safe_text(row.get('ts_code')),
            'name': safe_text(row.get('name')),
            'industry_primary': safe_text(row.get('industry_primary')),
            'industry_secondary': safe_text(row.get('industry_secondary')),
            'industry_bucket': safe_text(row.get('industry_bucket')),
            'mechanism_primary': mechanism_primary,
            'subchain_primary': subchain_primary,
            'core_driver_type': safe_text(row.get('core_driver_type')),
            'pricing_anchor': safe_text(row.get('pricing_anchor')),
            'secondary_exposures': '|'.join(exposures),
            'theme_primary': safe_text(row.get('theme_primary')),
            'liquidity_bucket': safe_text(row.get('liquidity_bucket')) or 'C',
            'board': safe_text(row.get('board')),
            'exchange': safe_text(row.get('exchange')),
            'mapping_confidence': round(mapping_confidence, 4),
            'exposure_count': len(exposures),
            'profile_score': profile_score,
            'customer_anchor': safe_text(row.get('customer_anchor')),
            'benefit_mode': safe_text(row.get('benefit_mode')),
            'spec_upgrade_level': safe_text(row.get('spec_upgrade_level')),
            'global_vs_domestic_exposure': safe_text(row.get('global_vs_domestic_exposure')),
            'resource_exposure': safe_text(row.get('resource_exposure')),
            'elasticity_bucket': safe_text(row.get('elasticity_bucket')),
            'cost_pass_through': safe_text(row.get('cost_pass_through')),
            'direct_resource_link': safe_text(row.get('direct_resource_link')),
            'inventory_sensitivity': safe_text(row.get('inventory_sensitivity')),
            'commodity_primary': safe_text(row.get('commodity_primary')),
            'downstream_pricing_power': safe_text(row.get('downstream_pricing_power')),
            'style_bucket': safe_text(row.get('style_bucket')),
            'duration_sensitivity': safe_text(row.get('duration_sensitivity')),
            'yield_sensitivity': safe_text(row.get('yield_sensitivity')),
            'macro_beta_bucket': safe_text(row.get('macro_beta_bucket')),
            'credit_sensitivity': safe_text(row.get('credit_sensitivity')),
            'risk_appetite_sensitivity': safe_text(row.get('risk_appetite_sensitivity')),
            'defensive_vs_offensive': safe_text(row.get('defensive_vs_offensive')),
            'notes': notes,
        }
        rows.append(payload)
    result = pd.DataFrame(rows)
    for field in STOCK_PROFILE_FIELDS:
        if field not in result.columns:
            result[field] = ''
    result = result.loc[result['mechanism_primary'].isin(MECHANISM_GROUPS)].copy()
    return result[STOCK_PROFILE_FIELDS].sort_values(['mechanism_primary', 'symbol']).reset_index(drop=True)


def load_taxonomy_lookup(contract_root_path: Path) -> Dict[str, Dict[str, Any]]:
    path = contract_root_path / 'event_taxonomy.json'
    payload = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = list(payload.get('mechanism_groups', []) or [])
    return {safe_text(row.get('event_type')): dict(row) for row in rows}


def load_source_contracts(contract_root_path: Path) -> Dict[str, Any]:
    path = contract_root_path / 'source_contracts.json'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def load_event_history(config: Dict[str, Any], structured_events: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    router_cfg = dict(config.get('industry_router', {}) or {})
    lookback_days = int(router_cfg.get('history_lookback_days', 14) or 14)
    event_store_path = Path(str(config['paths']['event_store_root'])) / 'event_store.jsonl'
    events: List[Dict[str, Any]] = []
    if event_store_path.exists():
        cutoff = datetime.now().date() - timedelta(days=lookback_days)
        with event_store_path.open('r', encoding='utf-8') as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except Exception:
                    continue
                date_text = parse_date(item.get('publish_time') or item.get('crawl_time'))
                if not date_text:
                    continue
                try:
                    item_date = datetime.strptime(date_text, '%Y-%m-%d').date()
                except Exception:
                    continue
                if item_date < cutoff:
                    continue
                events.append(item)
    elif structured_events:
        events.extend(list(structured_events))
    deduped: Dict[str, Dict[str, Any]] = {}
    for item in events:
        event_id = safe_text(item.get('event_id')) or f"fallback_{len(deduped) + 1}"
        deduped[event_id] = item
    ordered = list(deduped.values())
    ordered.sort(key=lambda row: (parse_date(row.get('publish_time') or row.get('crawl_time')), safe_text(row.get('event_id'))))
    return ordered
