from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from ...logging_utils import log_line
from ...research_fact_store import load_router_factor_context
from ..contracts import MECHANISM_GROUPS, MECHANISM_STATE_FIELDS, SIGNAL_FIELDS, STOCK_PROFILE_FIELDS
from ..mechanisms import get_mechanism_policies, get_policy_map
from .backtest_engine import run_signal_backtests
from .common import safe_float, safe_int, safe_text
from .event_pipeline import build_event_instances, build_event_stock_mapping
from .loaders import (
    build_stock_profile,
    contract_root,
    copy_contract_snapshot,
    load_event_history,
    load_source_contracts,
    load_taxonomy_lookup,
    output_root,
    resolve_mechanism_map,
    resolve_stock_master,
)
from .source_ingest import fetch_source_snapshots


def build_context_payload(stock_signal_df: pd.DataFrame, mechanism_state_df: pd.DataFrame, source_summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if stock_signal_df.empty:
        return {'status': 'empty'}
    latest_date = max(stock_signal_df['date'].astype(str).tolist())
    latest_df = stock_signal_df.loc[stock_signal_df['date'].astype(str) == latest_date].copy().sort_values('final_score', ascending=False)
    active_df = latest_df.loc[latest_df['signal_state'].astype(str).isin(['entry', 'hold'])].copy()
    signal_cols = [
        'symbol', 'mechanism_primary', 'industry_primary', 'final_score', 'event_score', 'source_state_score',
        'attribution_bucket', 'reason_top', 'dominant_state_driver', 'price_state_score',
        'inventory_state_score', 'macro_regime_score', 'dominant_source_driver',
    ]
    signal_cols = [col for col in signal_cols if col in active_df.columns]
    top_signals = active_df.head(8)[signal_cols].to_dict(orient='records')
    state_latest = mechanism_state_df.loc[mechanism_state_df['date'].astype(str) == latest_date].copy() if not mechanism_state_df.empty else pd.DataFrame()
    overview: List[Dict[str, Any]] = []
    for mechanism in MECHANISM_GROUPS:
        mech_signal = active_df.loc[active_df['mechanism_primary'] == mechanism].head(1)
        mech_state = state_latest.loc[(state_latest['mechanism_group'] == mechanism) & (state_latest['scope_type'] == 'mechanism')].head(1)
        overview.append(
            {
                'mechanism_group': mechanism,
                'top_signal_symbol': '' if mech_signal.empty else safe_text(mech_signal.iloc[0]['symbol']),
                'top_signal_score': 0.0 if mech_signal.empty else round(safe_float(mech_signal.iloc[0]['final_score']), 4),
                'state_score': 0.0 if mech_state.empty else round(safe_float(mech_state.iloc[0]['state_score']), 4),
                'source_state_score': 0.0 if mech_state.empty else round(safe_float(mech_state.iloc[0]['source_state_score']), 4),
                'heat_score': 0.0 if mech_state.empty else round(safe_float(mech_state.iloc[0]['heat_score']), 4),
                'evidence_count': 0 if mech_state.empty else safe_int(mech_state.iloc[0]['evidence_count']),
                'regime_label': '' if mech_state.empty else safe_text(mech_state.iloc[0]['regime_label']),
            }
        )
    source_overview = []
    for mechanism, row in dict((source_summary or {}).get('by_mechanism', {}) or {}).items():
        source_overview.append(
            {
                'mechanism_group': mechanism,
                'source_count': safe_int(row.get('source_count')),
                'avg_signal_score': round(safe_float(row.get('avg_signal_score')), 4),
                'top_sources': list(row.get('top_sources', []) or [])[:3],
            }
        )
    return {
        'latest_date': latest_date,
        'mechanism_overview': overview,
        'top_stock_signals': top_signals,
        'source_overview': source_overview,
    }


def _write_mechanism_sidecars(output_root_path: Path, mechanism_frames: Dict[str, Dict[str, pd.DataFrame]]) -> None:
    for mechanism, bundle in mechanism_frames.items():
        for kind, frame in bundle.items():
            path = output_root_path / f'{mechanism}_{kind}.csv'
            if frame.empty:
                if path.exists():
                    path.unlink()
                continue
            frame.to_csv(path, index=False, encoding='utf-8-sig')


def build_industry_router_artifacts(config: Dict[str, Any], structured_events: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    router_cfg = dict(config.get('industry_router', {}) or {})
    if not bool(router_cfg.get('enabled', False)):
        return {'enabled': False, 'status': 'disabled'}

    contract_root_path = contract_root(config)
    output_root_path = output_root(config)
    copy_contract_snapshot(contract_root_path=contract_root_path, output_root_path=output_root_path)
    legacy_state_path = output_root_path / 'industry_state_daily.csv'
    if legacy_state_path.exists():
        legacy_state_path.unlink()
    log_line(config, 'Industry Router: 开始构建三机制五轮统一研究产物')

    stock_master_df = resolve_stock_master(config=config, contract_root_path=contract_root_path)
    mechanism_map_df = resolve_mechanism_map(contract_root_path=contract_root_path)
    stock_profile_df = build_stock_profile(stock_master_df=stock_master_df, mechanism_map_df=mechanism_map_df)
    taxonomy_lookup = load_taxonomy_lookup(contract_root_path=contract_root_path)
    source_contracts = load_source_contracts(contract_root_path=contract_root_path)
    events = load_event_history(config=config, structured_events=structured_events)
    policy_map = get_policy_map()

    event_instances_df = build_event_instances(events=events, taxonomy_lookup=taxonomy_lookup, stock_profile_df=stock_profile_df, policy_map=policy_map)
    if event_instances_df.empty:
        event_instances_df = pd.DataFrame(
            columns=['date', 'mechanism_group', 'affected_industry', 'event_type', 'direction', 'strength', 'confidence']
        )
    as_of_candidates = event_instances_df['date'].astype(str).tolist() if not event_instances_df.empty else []
    if not as_of_candidates:
        as_of_candidates = [safe_text(item.get('publish_time') or item.get('crawl_time'))[:10] for item in events if safe_text(item.get('publish_time') or item.get('crawl_time'))]
    as_of_date = max(as_of_candidates) if as_of_candidates else datetime.now().strftime('%Y-%m-%d')
    sql_factor_context = load_router_factor_context(config=config, as_of_date=as_of_date)
    source_result = fetch_source_snapshots(config=config, source_contracts=source_contracts, output_root=output_root_path, as_of_date=as_of_date)
    source_state_df = pd.DataFrame(list(source_result.get('state_rows', []) or []))
    if source_state_df.empty:
        source_state_df = pd.DataFrame(columns=['date', 'mechanism_group', 'source_id', 'source_name', 'category', 'source_signal_score', 'confidence', 'publish_date', 'title', 'summary', 'url', 'source_weight', 'category_weight', 'freshness_weight', 'positive_hits', 'negative_hits'])
    mapping_df = build_event_stock_mapping(event_instances_df=event_instances_df, stock_profile_df=stock_profile_df, policy_map=policy_map)
    if mapping_df.empty:
        mapping_df = pd.DataFrame(columns=['mechanism_primary'])

    mechanism_frames: Dict[str, Dict[str, pd.DataFrame]] = {}
    profile_frames: List[pd.DataFrame] = []
    state_frames: List[pd.DataFrame] = []
    core_frames: List[pd.DataFrame] = []
    signal_frames: List[pd.DataFrame] = []
    price_root_text = str(config.get('market_pipeline', {}).get('enriched_dir', '') or '').strip()
    price_root = Path(price_root_text) if price_root_text else None
    price_cache: Dict[str, pd.DataFrame] = {}
    for policy in get_mechanism_policies():
        mech_source_df = source_state_df.loc[source_state_df['mechanism_group'] == policy.name].copy()
        profile_df = policy.build_profile(stock_profile_df=stock_profile_df, mechanism_map_df=mechanism_map_df)
        source_context = policy.build_source_context(source_state_df=mech_source_df, as_of_date=as_of_date, context={'output_root': str(output_root_path)})
        state_df = policy.build_state(
            raw_inputs={'event_instances_df': event_instances_df, 'mapping_df': mapping_df, 'stock_profile_df': stock_profile_df},
            source_state=mech_source_df,
            context={'as_of_date': as_of_date, 'source_context': source_context, 'sql_factor_context': dict(sql_factor_context.get(policy.name, {}) or {})},
        )
        core_df = policy.build_core_variables(
            state_df=state_df,
            profile_df=profile_df,
            event_rows=event_instances_df.loc[event_instances_df['mechanism_group'] == policy.name].copy(),
            context={
                'mapping_df': mapping_df.loc[mapping_df['mechanism_primary'] == policy.name].copy(),
                'price_root': price_root,
                'price_cache': price_cache,
                'sql_factor_context': dict(sql_factor_context.get(policy.name, {}) or {}),
            },
        )
        signal_df = policy.generate_signal(
            core_variables=core_df,
            base_inputs={'event_instances_df': event_instances_df, 'mapping_df': mapping_df},
            context={'price_root': price_root, 'price_cache': price_cache},
        )
        mechanism_frames[policy.name] = {'profile': profile_df, 'state': state_df, 'core_variable': core_df, 'signal': signal_df}
        if not profile_df.empty:
            profile_frames.append(profile_df)
        if not state_df.empty:
            state_frames.append(state_df)
        if not core_df.empty:
            core_frames.append(core_df)
        if not signal_df.empty:
            signal_frames.append(signal_df)

    runtime_profile_df = pd.concat(profile_frames, ignore_index=True, sort=False) if profile_frames else pd.DataFrame(columns=STOCK_PROFILE_FIELDS)
    mechanism_state_df = pd.concat(state_frames, ignore_index=True, sort=False) if state_frames else pd.DataFrame(columns=MECHANISM_STATE_FIELDS)
    core_variable_df = pd.concat(core_frames, ignore_index=True, sort=False) if core_frames else pd.DataFrame()
    signal_df = pd.concat(signal_frames, ignore_index=True, sort=False) if signal_frames else pd.DataFrame(columns=SIGNAL_FIELDS)
    if not signal_df.empty:
        signal_df = signal_df.sort_values(['date', 'final_score', 'symbol'], ascending=[True, False, True]).reset_index(drop=True)

    stock_master_path = output_root_path / 'stock_master.csv'
    mechanism_map_path = output_root_path / 'mechanism_map.csv'
    stock_profile_path = output_root_path / 'stock_profile.csv'
    event_instances_path = output_root_path / 'event_instances.csv'
    mapping_path = output_root_path / 'event_stock_mapping.csv'
    mechanism_state_path = output_root_path / 'mechanism_state_daily.csv'
    source_state_path = output_root_path / 'source_state_daily.csv'
    core_variable_path = output_root_path / 'core_variable_daily.csv'
    signal_path = output_root_path / 'stock_signal_daily.csv'
    latest_signal_path = output_root_path / 'latest_stock_signal.csv'
    summary_path = output_root_path / 'industry_router_summary.json'

    stock_master_df.to_csv(stock_master_path, index=False, encoding='utf-8-sig')
    mechanism_map_df.to_csv(mechanism_map_path, index=False, encoding='utf-8-sig')
    runtime_profile_df.to_csv(stock_profile_path, index=False, encoding='utf-8-sig')
    event_instances_df.to_csv(event_instances_path, index=False, encoding='utf-8-sig')
    mapping_df.to_csv(mapping_path, index=False, encoding='utf-8-sig')
    mechanism_state_df.to_csv(mechanism_state_path, index=False, encoding='utf-8-sig')
    source_state_df.to_csv(source_state_path, index=False, encoding='utf-8-sig')
    core_variable_df.to_csv(core_variable_path, index=False, encoding='utf-8-sig')
    signal_df.to_csv(signal_path, index=False, encoding='utf-8-sig')
    if not signal_df.empty:
        latest_date = max(signal_df['date'].astype(str).tolist())
        signal_df.loc[signal_df['date'].astype(str) == latest_date].sort_values('final_score', ascending=False).to_csv(latest_signal_path, index=False, encoding='utf-8-sig')
    else:
        pd.DataFrame(columns=['symbol', 'date', 'final_score']).to_csv(latest_signal_path, index=False, encoding='utf-8-sig')
    _write_mechanism_sidecars(output_root_path=output_root_path, mechanism_frames=mechanism_frames)

    backtest_result = run_signal_backtests(config=config, signal_df=signal_df, output_root=output_root_path, policy_map=policy_map) if bool(router_cfg.get('enable_backtest', True)) else {}
    context_payload = build_context_payload(stock_signal_df=signal_df, mechanism_state_df=mechanism_state_df, source_summary=dict(source_result.get('summary', {}) or {}))
    summary = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'enabled': True,
        'contract_version': safe_text(source_contracts.get('contract_version')) or 'industry_router_unified_phase1',
        'mechanisms': list(MECHANISM_GROUPS),
        'history_events': int(len(events)),
        'event_instances': int(len(event_instances_df)),
        'event_stock_mappings': int(len(mapping_df)),
        'stock_profile_rows': int(len(runtime_profile_df)),
        'mechanism_state_rows': int(len(mechanism_state_df)),
        'core_variable_rows': int(len(core_variable_df)),
        'source_state_rows': int(len(source_state_df)),
        'source_snapshot_ok_count': int(dict(source_result.get('summary', {}) or {}).get('ok_count') or 0),
        'source_snapshot_error_count': int(dict(source_result.get('summary', {}) or {}).get('error_count') or 0),
        'signal_rows': int(len(signal_df)),
        'latest_signal_date': '' if signal_df.empty else max(signal_df['date'].astype(str).tolist()),
        'per_mechanism_rows': {
            name: {
                'profile': int(len(bundle.get('profile', pd.DataFrame()))),
                'state': int(len(bundle.get('state', pd.DataFrame()))),
                'core_variable': int(len(bundle.get('core_variable', pd.DataFrame()))),
                'signal': int(len(bundle.get('signal', pd.DataFrame()))),
            }
            for name, bundle in mechanism_frames.items()
        },
        'paths': {
            'stock_master': str(stock_master_path),
            'mechanism_map': str(mechanism_map_path),
            'stock_profile': str(stock_profile_path),
            'event_instances': str(event_instances_path),
            'event_stock_mapping': str(mapping_path),
            'mechanism_state_daily': str(mechanism_state_path),
            'source_state_daily': str(source_state_path),
            'core_variable_daily': str(core_variable_path),
            'stock_signal_daily': str(signal_path),
            'latest_stock_signal': str(latest_signal_path),
            'source_snapshot_index': safe_text(dict(source_result.get('summary', {}) or {}).get('index_path')),
            'source_snapshot_items': safe_text(dict(source_result.get('summary', {}) or {}).get('items_path')),
        },
        'source_fetch': dict(source_result.get('summary', {}) or {}),
        'context_payload': context_payload,
        'backtest': {
            'combined_status': safe_text(backtest_result.get('combined_report', {}).get('status')),
            'mechanism_status': {key: safe_text(value.get('status')) for key, value in dict(backtest_result.get('reports', {}) or {}).items()},
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    log_line(config, f"Industry Router: 完成 deepened_mechanisms={len(MECHANISM_GROUPS)} event_instances={len(event_instances_df)} mappings={len(mapping_df)} signal_rows={len(signal_df)} summary={summary_path}")
    return {
        'enabled': True,
        'status': 'ok',
        'summary_path': str(summary_path),
        'latest_signal_path': str(latest_signal_path),
        'context_payload': context_payload,
        'summary': summary,
    }
