from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping

import pandas as pd

from ...config_utils import ensure_dir
from ..contracts import BACKTEST_TRADE_FIELDS, MECHANISM_GROUPS
from .calendar_align import find_trade_index, forward_slice, load_price_frame
from .common import normalize_symbol, safe_float, safe_text
from .metrics import summarize_horizon
from .signal_loader import select_entry_candidates, summarize_attribution


def simulate_trade_path(policy: Any, row: Dict[str, Any], price_df: pd.DataFrame, horizon_days: int, signal_lookup: Dict[tuple[str, str], Dict[str, Any]]) -> Dict[str, Any] | None:
    if price_df.empty:
        return None
    signal_date = str(row['date'])
    entry_idx = find_trade_index(price_df, signal_date)
    if entry_idx is None or entry_idx >= len(price_df) - 1:
        return None
    entry_row = price_df.iloc[entry_idx]
    entry_close = safe_float(entry_row.get('close'), 0.0)
    if entry_close <= 0:
        return None
    trail = forward_slice(price_df, entry_idx, max(1, int(horizon_days)))
    if len(trail) <= 1:
        return None
    exit_row = trail.iloc[-1]
    exit_idx = entry_idx + len(trail) - 1
    for offset in range(1, len(trail)):
        current = trail.iloc[offset]
        current_close = safe_float(current.get('close'), 0.0)
        if current_close <= 0:
            continue
        current_date = safe_text(current.get('date'))
        forward_return = current_close / entry_close - 1.0
        future_signal = signal_lookup.get((safe_text(row.get('symbol')), current_date), {})
        ctx = {
            **row,
            **future_signal,
            'days_held': int(offset),
            'horizon_days': int(horizon_days),
            'forward_return': round(forward_return, 6),
        }
        exit_now = policy.exit_rule(ctx, context={'phase': 'backtest', 'future_signal': future_signal})
        hold_now = policy.hold_rule(ctx, context={'phase': 'backtest', 'future_signal': future_signal})
        exit_row = current
        exit_idx = entry_idx + offset
        if exit_now or not hold_now:
            break
    exit_close = safe_float(exit_row.get('close'), 0.0)
    if exit_close <= 0:
        return None
    return {
        'entry_date': safe_text(entry_row.get('date')),
        'exit_date': safe_text(exit_row.get('date')),
        'days_held': int(exit_idx - entry_idx),
        'forward_return': round(exit_close / entry_close - 1.0, 6),
    }


def run_signal_backtests(config: Dict[str, Any], signal_df: pd.DataFrame, output_root: Path, policy_map: Mapping[str, Any]) -> Dict[str, Any]:
    router_cfg = dict(config.get('industry_router', {}) or {})
    backtest_cfg = dict(router_cfg.get('backtest', {}) or {})
    price_root_text = str(config.get('market_pipeline', {}).get('enriched_dir', '') or '').strip()
    price_root = Path(price_root_text) if price_root_text else None
    horizons = [int(x) for x in list(backtest_cfg.get('horizons', [1, 2]) or [1, 2]) if int(x) > 0]
    top_k = int(backtest_cfg.get('top_k', 3) or 3)
    backtest_root = ensure_dir(output_root / 'backtests')
    price_cache: Dict[str, pd.DataFrame] = {}
    reports: Dict[str, Any] = {}
    attribution_rows: list[Dict[str, Any]] = []
    combined_details: list[Dict[str, Any]] = []

    if signal_df.empty:
        payload = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'empty_signal_table',
            'mechanisms': list(MECHANISM_GROUPS),
        }
        (backtest_root / 'backtest_attribution_summary.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'reports': {}, 'combined_report': payload, 'attribution_report': payload}

    signal_df = signal_df.copy()
    signal_df['date'] = signal_df['date'].astype(str).str.slice(0, 10)
    signal_df['symbol'] = signal_df['symbol'].map(normalize_symbol)
    signal_lookup = {(safe_text(row['symbol']), safe_text(row['date'])): row.to_dict() for _, row in signal_df.iterrows()}

    for mechanism in MECHANISM_GROUPS:
        policy = policy_map[mechanism]
        mech_df = signal_df.loc[signal_df['mechanism_primary'] == mechanism].copy()
        for horizon in horizons:
            detail_path = backtest_root / f'backtest_{mechanism}_h{horizon}_details.csv'
            if detail_path.exists():
                detail_path.unlink()
        if mech_df.empty:
            report = {
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mechanism_group': mechanism,
                'status': 'no_active_signals',
                'top_k': top_k,
                'entry_rule': 'policy.entry_rule',
                'horizons': {str(h): summarize_horizon(pd.DataFrame()) for h in horizons},
            }
            (backtest_root / f'backtest_{mechanism}_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            reports[mechanism] = report
            continue
        picked = select_entry_candidates(signal_df=mech_df, policy=policy, top_k=top_k)
        if picked.empty:
            report = {
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mechanism_group': mechanism,
                'status': 'no_entry_candidates',
                'top_k': top_k,
                'entry_rule': 'policy.entry_rule',
                'horizons': {str(h): summarize_horizon(pd.DataFrame()) for h in horizons},
            }
            (backtest_root / f'backtest_{mechanism}_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            reports[mechanism] = report
            continue
        detail_frames: Dict[int, list[Dict[str, Any]]] = {h: [] for h in horizons}
        candidate_attr = (
            picked.groupby(['attribution_bucket', 'attribution_label'], dropna=False)
            .size()
            .reset_index(name='candidate_count')
        )
        for _, row in picked.iterrows():
            price_df = load_price_frame(price_root=price_root, symbol=str(row['symbol']), cache=price_cache) if price_root else pd.DataFrame()
            for horizon in horizons:
                trade = simulate_trade_path(policy=policy, row=row.to_dict(), price_df=price_df, horizon_days=min(horizon, int(getattr(policy, 'tuning').max_horizon_days)), signal_lookup=signal_lookup)
                if trade is None:
                    continue
                item = {
                    'mechanism_group': mechanism,
                    'signal_date': str(row['date']),
                    'symbol': str(row['symbol']),
                    'final_score': safe_float(row['final_score']),
                    'signal_state': str(row.get('signal_state', 'entry')),
                    'attribution_bucket': str(row.get('attribution_bucket', 'state')),
                    'attribution_label': str(row.get('attribution_label', mechanism)),
                    'reason_top': str(row.get('reason_top', '')),
                    'horizon_days': int(horizon),
                    **trade,
                }
                detail_frames[horizon].append(item)
                if horizon == horizons[0]:
                    combined_details.append(item)
        horizon_reports: Dict[str, Any] = {}
        for horizon in horizons:
            detail_df = pd.DataFrame(detail_frames[horizon])
            detail_path = backtest_root / f'backtest_{mechanism}_h{horizon}_details.csv'
            if not detail_df.empty:
                for field in BACKTEST_TRADE_FIELDS:
                    if field not in detail_df.columns:
                        detail_df[field] = ''
                detail_df[BACKTEST_TRADE_FIELDS].to_csv(detail_path, index=False, encoding='utf-8-sig')
                attr_df = summarize_attribution(detail_df)
            else:
                attr_df = pd.DataFrame()
            if not attr_df.empty:
                attr_df['mechanism_group'] = mechanism
                attr_df['horizon_days'] = int(horizon)
                attr_df['candidate_count'] = attr_df['trade_count']
                attribution_rows.extend(attr_df.to_dict(orient='records'))
            elif not candidate_attr.empty:
                empty_attr = candidate_attr.copy()
                empty_attr['trade_count'] = 0
                empty_attr['avg_forward_return'] = 0.0
                empty_attr['mechanism_group'] = mechanism
                empty_attr['horizon_days'] = int(horizon)
                attribution_rows.extend(empty_attr.to_dict(orient='records'))
            horizon_reports[str(horizon)] = {
                **summarize_horizon(detail_df),
                'detail_path': str(detail_path),
            }
        report = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'mechanism_group': mechanism,
            'status': 'ok',
            'top_k': top_k,
            'entry_rule': 'policy.entry_rule',
            'signal_days_available': int(picked['date'].nunique()),
            'unique_symbols': int(picked['symbol'].nunique()),
            'horizons': horizon_reports,
        }
        (backtest_root / f'backtest_{mechanism}_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        reports[mechanism] = report

    combined_df = pd.DataFrame(combined_details)
    combined_report = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'ok' if not combined_df.empty else 'insufficient_history',
        'default_horizon_days': int(horizons[0]) if horizons else 0,
        'summary': summarize_horizon(combined_df),
        'mechanisms': list(MECHANISM_GROUPS),
    }
    (backtest_root / 'backtest_combined_summary.json').write_text(json.dumps(combined_report, ensure_ascii=False, indent=2), encoding='utf-8')
    combined_detail_path = backtest_root / 'backtest_combined_details.csv'
    if not combined_df.empty:
        for field in BACKTEST_TRADE_FIELDS:
            if field not in combined_df.columns:
                combined_df[field] = ''
        combined_df[BACKTEST_TRADE_FIELDS].to_csv(combined_detail_path, index=False, encoding='utf-8-sig')
    elif combined_detail_path.exists():
        combined_detail_path.unlink()

    attribution_df = pd.DataFrame(attribution_rows)
    attribution_report = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'ok' if not attribution_df.empty else 'empty',
        'rows': attribution_df.to_dict(orient='records'),
    }
    (backtest_root / 'backtest_attribution_summary.json').write_text(json.dumps(attribution_report, ensure_ascii=False, indent=2), encoding='utf-8')
    attribution_csv_path = backtest_root / 'backtest_attribution_summary.csv'
    if not attribution_df.empty:
        attribution_df.to_csv(attribution_csv_path, index=False, encoding='utf-8-sig')
    elif attribution_csv_path.exists():
        attribution_csv_path.unlink()

    return {
        'reports': reports,
        'combined_report': combined_report,
        'attribution_report': attribution_report,
    }
