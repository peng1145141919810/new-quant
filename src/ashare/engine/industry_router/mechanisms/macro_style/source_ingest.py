from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from ...core.common import clip, mean_or_zero, safe_float, safe_text, sign_consensus


def summarize_source_state(source_state_df: pd.DataFrame, as_of_date: str, tuning, config, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if source_state_df.empty or not {"mechanism_group", "date"}.issubset(set(source_state_df.columns)):
        return {
            "by_date": {
                safe_text(as_of_date): {
                    "macro_regime_score": 0.0,
                    "liquidity_condition_score": 0.0,
                    "risk_appetite_score": 0.0,
                    "source_consensus_score": 0.0,
                    "confidence": 0.0,
                    "heat_score": 0.0,
                    "source_state_score": 0.0,
                    "key_driver_1": "",
                    "key_driver_2": "",
                    "source_count": 0,
                }
            }
        }
    subset = source_state_df.loc[source_state_df['mechanism_group'] == tuning.mechanism_group].copy() if not source_state_df.empty else pd.DataFrame()
    dates = sorted(set(subset['date'].astype(str).tolist()) or [safe_text(as_of_date)])
    by_date: Dict[str, Dict[str, Any]] = {}
    for date_text in dates:
        group = subset.loc[subset['date'].astype(str) == date_text].copy()
        industry_group = group.loc[group['category'] == 'industry_state_sources'].copy()
        macro_group = group.loc[group['category'] == 'macro_context_sources'].copy()
        industry_scores = pd.to_numeric(industry_group['source_signal_score'], errors='coerce').fillna(0.0).tolist()
        macro_scores = pd.to_numeric(macro_group['source_signal_score'], errors='coerce').fillna(0.0).tolist()
        all_scores = pd.to_numeric(group['source_signal_score'], errors='coerce').fillna(0.0).tolist()
        top = group.assign(abs_score=pd.to_numeric(group['source_signal_score'], errors='coerce').fillna(0.0).abs()).sort_values('abs_score', ascending=False)
        by_date[date_text] = {
            'macro_regime_score': round(clip(mean_or_zero(industry_scores) * 2.2, -1.0, 1.0), 4),
            'liquidity_condition_score': round(clip(0.60 * mean_or_zero(industry_scores) + 0.40 * mean_or_zero(macro_scores), -1.0, 1.0), 4),
            'risk_appetite_score': round(clip(mean_or_zero(macro_scores) * 2.0, -1.0, 1.0), 4),
            'source_consensus_score': round(clip(sign_consensus(all_scores) * (1 if mean_or_zero(all_scores) >= 0 else -1), -1.0, 1.0), 4),
            'confidence': round(clip(mean_or_zero(pd.to_numeric(group['confidence'], errors='coerce').fillna(0.0).tolist()), 0.0, 1.0), 4),
            'heat_score': round(clip(abs(mean_or_zero(all_scores)) + 0.05 * min(len(group), 4), 0.0, 1.0), 4),
            'source_state_score': round(clip(mean_or_zero(all_scores), -1.0, 1.0), 4),
            'key_driver_1': safe_text(top.iloc[0]['source_name']) if len(top) >= 1 else '',
            'key_driver_2': safe_text(top.iloc[1]['source_name']) if len(top) >= 2 else '',
            'source_count': int(len(group)),
        }
    return {'by_date': by_date}
