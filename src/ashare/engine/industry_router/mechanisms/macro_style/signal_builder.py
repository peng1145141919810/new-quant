from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from ...contracts import SIGNAL_FIELDS
from ...core.calendar_align import basket_relative_strength, compute_price_feature_snapshot, load_price_frame
from ...core.common import DEFENSIVE_BUCKET, LOW_MID_HIGH, STYLE_BUCKET, clip, map_bucket_score, safe_float, safe_json_text, safe_text


def build_core_variables(state_df: pd.DataFrame, profile_df: pd.DataFrame, event_rows: pd.DataFrame | None = None, context: Dict[str, Any] | None = None, tuning=None, config=None) -> pd.DataFrame:
    context = context or {}
    event_rows = event_rows if event_rows is not None else pd.DataFrame()
    mapping_df = context.get('mapping_df', pd.DataFrame())
    price_root = context.get('price_root')
    price_cache = context.setdefault('price_cache', {})
    mechanism_state = state_df.loc[state_df['scope_type'] == 'mechanism'].copy() if not state_df.empty else pd.DataFrame()
    industry_state = state_df.loc[state_df['scope_type'] == 'industry'].copy() if not state_df.empty else pd.DataFrame()
    event_lookup = {str(row['event_id']): row for _, row in event_rows.iterrows()} if not event_rows.empty else {}
    basket_symbols = profile_df['symbol'].astype(str).tolist()
    rows: List[Dict[str, Any]] = []
    for date_text in sorted(set(state_df['date'].astype(str).tolist()) if not state_df.empty else []):
        mech_row = mechanism_state.loc[mechanism_state['date'].astype(str) == date_text].head(1)
        mech_state = {} if mech_row.empty else mech_row.iloc[0].to_dict()
        basket_strength = basket_relative_strength(price_root=price_root, symbols=basket_symbols, trade_date=date_text, cache=price_cache, lookback=5) if price_root else 0.0
        for _, stock in profile_df.iterrows():
            symbol = safe_text(stock.get('symbol'))
            industry = safe_text(stock.get('industry_primary'))
            industry_row = industry_state.loc[(industry_state['date'].astype(str) == date_text) & (industry_state['scope_key'].astype(str) == industry)].head(1)
            industry_payload = {} if industry_row.empty else industry_row.iloc[0].to_dict()
            mapped = mapping_df.loc[(mapping_df['date'].astype(str) == date_text) & (mapping_df['symbol'].astype(str) == symbol)].copy() if not mapping_df.empty else pd.DataFrame()
            event_score = 0.0
            dominant_event_label = ''
            mapping_score = 0.0
            if not mapped.empty:
                mapping_score = float(pd.to_numeric(mapped['mapping_score'], errors='coerce').fillna(0.0).mean())
                weighted_events: List[tuple[float, str]] = []
                for _, item in mapped.iterrows():
                    event = event_lookup.get(str(item.get('event_id')))
                    if event is None:
                        continue
                    signed = safe_float(item.get('mapping_score'), 0.0) * safe_float(event.get('strength'), 0.0) * safe_float(event.get('confidence'), 0.0)
                    if safe_text(event.get('direction')).lower() == 'negative':
                        signed *= -1.0
                    event_score += 0.25 * signed
                    weighted_events.append((abs(signed), f"{safe_text(event.get('event_type'))}:{safe_text(item.get('mapping_reason'))}"))
                if weighted_events:
                    dominant_event_label = sorted(weighted_events, key=lambda x: (-x[0], x[1]))[0][1]
            price_snapshot = compute_price_feature_snapshot(load_price_frame(price_root=price_root, symbol=symbol, cache=price_cache) if price_root else pd.DataFrame(), date_text)
            basket_fit_score = clip(
                0.32 * map_bucket_score(stock.get('style_bucket'), STYLE_BUCKET, 0.45)
                + 0.20 * map_bucket_score(stock.get('duration_sensitivity'), LOW_MID_HIGH, 0.45)
                + 0.18 * map_bucket_score(stock.get('yield_sensitivity'), LOW_MID_HIGH, 0.45)
                + 0.16 * map_bucket_score(stock.get('macro_beta_bucket'), LOW_MID_HIGH, 0.45)
                + 0.14 * map_bucket_score(stock.get('defensive_vs_offensive'), DEFENSIVE_BUCKET, 0.5),
                0.0,
                1.0,
            )
            style_flow_score = clip(0.60 * safe_float(mech_state.get('style_rotation_score'), 0.0) + 0.40 * basket_strength, -1.0, 1.0)
            macro_regime_score = safe_float(mech_state.get('macro_regime_score'), 0.0)
            state_score = clip(0.44 * safe_float(industry_payload.get('state_score'), 0.0) + 0.56 * safe_float(mech_state.get('state_score'), 0.0), -1.0, 1.0)
            rows.append(
                {
                    'symbol': symbol,
                    'date': date_text,
                    'mechanism_primary': tuning.mechanism_group,
                    'industry_primary': industry,
                    'subchain_primary': safe_text(stock.get('subchain_primary')),
                    'base_score': round(0.08 + 0.22 * safe_float(stock.get('profile_score'), 0.0), 4),
                    'state_score': round(state_score, 4),
                    'industry_state_score': round(safe_float(industry_payload.get('state_score'), 0.0), 4),
                    'mechanism_state_score': round(safe_float(mech_state.get('state_score'), 0.0), 4),
                    'event_state_score': round(safe_float(industry_payload.get('event_state_score'), safe_float(mech_state.get('event_state_score'), 0.0)), 4),
                    'source_state_score': round(safe_float(mech_state.get('source_state_score'), 0.0), 4),
                    'event_score': round(event_score, 4),
                    'mapping_score': round(mapping_score, 4),
                    'profile_score': round(safe_float(stock.get('profile_score'), 0.0), 4),
                    'heat_score': round(max(safe_float(industry_payload.get('heat_score'), 0.0), safe_float(mech_state.get('heat_score'), 0.0)), 4),
                    'exposure_score': round(basket_fit_score, 4),
                    'price_state_score': 0.0,
                    'inventory_state_score': 0.0,
                    'macro_regime_score': round(macro_regime_score, 4),
                    'style_flow_score': round(style_flow_score, 4),
                    'basket_fit_score': round(basket_fit_score, 4),
                    'dominant_event_label': dominant_event_label,
                    'dominant_state_driver': safe_text(industry_payload.get('key_driver_1')) or safe_text(mech_state.get('key_driver_1')),
                    'dominant_source_driver': safe_text(mech_state.get('key_driver_1')),
                    'style_bucket': safe_text(stock.get('style_bucket')),
                    'duration_sensitivity': safe_text(stock.get('duration_sensitivity')),
                    'industry_bucket': safe_text(stock.get('industry_bucket')),
                    'defensive_vs_offensive': safe_text(stock.get('defensive_vs_offensive')),
                    'pre_3d_return': round(safe_float(price_snapshot.get('pre_3d_return'), 0.0), 6),
                    'pre_5d_return': round(safe_float(price_snapshot.get('pre_5d_return'), 0.0), 6),
                    'pre_10d_return': round(safe_float(price_snapshot.get('pre_10d_return'), 0.0), 6),
                    'amount_ratio_5d': round(safe_float(price_snapshot.get('amount_ratio_5d'), 1.0), 6),
                    'volume_ratio': round(safe_float(price_snapshot.get('volume_ratio'), 1.0), 6),
                    'pct_chg': round(safe_float(price_snapshot.get('pct_chg'), 0.0), 6),
                    'drawup_10d': round(safe_float(price_snapshot.get('drawup_10d'), 0.0), 6),
                    'basket_relative_strength': round(basket_strength, 6),
                }
            )
    return pd.DataFrame(rows)


def generate_signal(core_variables: pd.DataFrame, base_inputs: Dict[str, Any], context: Dict[str, Any] | None = None, tuning=None, config=None, risk_filter=None, attribution_bucket=None, attribution_label=None) -> pd.DataFrame:
    if core_variables.empty:
        return pd.DataFrame(columns=SIGNAL_FIELDS)
    signal_weights = dict(config.get('signal_weights', {}) or {})
    rows: List[Dict[str, Any]] = []
    for _, row in core_variables.iterrows():
        pre_risk = (
            signal_weights.get('base', 0.10) * safe_float(row.get('base_score'), 0.0)
            + signal_weights.get('state', 0.28) * safe_float(row.get('state_score'), 0.0)
            + signal_weights.get('macro_regime', 0.22) * safe_float(row.get('macro_regime_score'), 0.0)
            + signal_weights.get('style_flow', 0.18) * safe_float(row.get('style_flow_score'), 0.0)
            + signal_weights.get('basket_fit', 0.16) * safe_float(row.get('basket_fit_score'), 0.0)
            + signal_weights.get('event', 0.06) * safe_float(row.get('event_score'), 0.0)
        )
        risk = risk_filter(row.to_dict(), context=context or {})
        confirmation_bonus = safe_float(risk.get('confirmation_bonus'), 0.0)
        penalty = safe_float(risk.get('penalty'), 0.0)
        final_score = round(pre_risk + confirmation_bonus - penalty, 4)
        allow_entry = bool(risk.get('allow_entry'))
        veto_triggered = bool(risk.get('veto_triggered'))
        state_score = safe_float(row.get('state_score'), 0.0)
        if veto_triggered or state_score <= tuning.negative_state_exit or final_score <= tuning.exit_score:
            signal_state = 'exit'
        elif allow_entry and final_score >= tuning.entry_score:
            signal_state = 'entry'
        elif final_score >= tuning.hold_score:
            signal_state = 'hold'
        else:
            signal_state = 'watch'
        payload = row.to_dict()
        payload['final_score'] = final_score
        bucket = attribution_bucket(payload)
        label = attribution_label(payload)
        reason_top = safe_text(row.get('dominant_event_label')) or f"{bucket}:{label}"
        output = {
            **row.to_dict(),
            'confirmation_score': round(confirmation_bonus, 4),
            'risk_penalty': round(penalty, 4),
            'pre_risk_score': round(pre_risk, 4),
            'final_score': final_score,
            'penalty_score': round(penalty, 4),
            'confirmation_bonus': round(confirmation_bonus, 4),
            'veto_triggered': veto_triggered,
            'veto_reason': safe_text(risk.get('veto_reason')),
            'signal_state': signal_state,
            'allow_entry': allow_entry,
            'attribution_bucket': bucket,
            'attribution_label': label,
            'reason_top': reason_top,
            'confirmation_flags': '|'.join(risk.get('confirmation_flags', [])),
            'risk_flags': '|'.join(risk.get('flags', [])),
            'penalty_detail_json': safe_json_text(risk.get('penalty_detail', {})),
            'confirmation_detail_json': safe_json_text(risk.get('confirmation_detail', {})),
            'profile_context_json': safe_json_text(
                {
                    'style_bucket': safe_text(row.get('style_bucket')),
                    'duration_sensitivity': safe_text(row.get('duration_sensitivity')),
                    'industry_bucket': safe_text(row.get('industry_bucket')),
                    'defensive_vs_offensive': safe_text(row.get('defensive_vs_offensive')),
                }
            ),
        }
        for field in SIGNAL_FIELDS:
            output.setdefault(field, '' if field in {'symbol', 'date', 'mechanism_primary', 'industry_primary', 'subchain_primary', 'signal_state', 'attribution_bucket', 'attribution_label', 'reason_top', 'confirmation_flags', 'risk_flags', 'penalty_detail_json', 'confirmation_detail_json', 'profile_context_json', 'veto_reason'} else 0.0)
        rows.append(output)
    return pd.DataFrame(rows)
