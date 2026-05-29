from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from ...core.common import clip, mean_or_zero, safe_float, safe_text, sign_consensus


def _hit_balance(group: pd.DataFrame) -> float:
    pos = group['positive_hits'].astype(str).map(lambda x: 0 if not x else len([item for item in x.split('|') if item])).sum()
    neg = group['negative_hits'].astype(str).map(lambda x: 0 if not x else len([item for item in x.split('|') if item])).sum()
    total = max(pos + neg, 1)
    return clip((float(pos) - float(neg)) / float(total), -1.0, 1.0)


def summarize_source_state(source_state_df: pd.DataFrame, as_of_date: str, tuning, config, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if source_state_df.empty or not {"mechanism_group", "date"}.issubset(set(source_state_df.columns)):
        return {
            "by_date": {
                safe_text(as_of_date): {
                    "industry_expansion_score": 0.0,
                    "external_demand_score": 0.0,
                    "source_consensus_score": 0.0,
                    "hit_balance_score": 0.0,
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
        score_values = pd.to_numeric(group['source_signal_score'], errors='coerce').fillna(0.0).tolist() if not group.empty else []
        top = group.assign(abs_score=pd.to_numeric(group['source_signal_score'], errors='coerce').fillna(0.0).abs()).sort_values('abs_score', ascending=False)
        by_date[date_text] = {
            'industry_expansion_score': round(clip(mean_or_zero(pd.to_numeric(industry_group['source_signal_score'], errors='coerce').fillna(0.0).tolist()) * 2.2, -1.0, 1.0), 4),
            'external_demand_score': round(clip(mean_or_zero(pd.to_numeric(macro_group['source_signal_score'], errors='coerce').fillna(0.0).tolist()) * 2.0, -1.0, 1.0), 4),
            'source_consensus_score': round(clip(sign_consensus(score_values) * (1 if mean_or_zero(score_values) >= 0 else -1), -1.0, 1.0), 4),
            'hit_balance_score': round(_hit_balance(group), 4) if not group.empty else 0.0,
            'confidence': round(clip(mean_or_zero(pd.to_numeric(group['confidence'], errors='coerce').fillna(0.0).tolist()), 0.0, 1.0), 4),
            'heat_score': round(clip(abs(mean_or_zero(score_values)) + 0.05 * min(len(group), 4), 0.0, 1.0), 4),
            'source_state_score': round(clip(mean_or_zero(score_values), -1.0, 1.0), 4),
            'key_driver_1': safe_text(top.iloc[0]['source_name']) if len(top) >= 1 else '',
            'key_driver_2': safe_text(top.iloc[1]['source_name']) if len(top) >= 2 else '',
            'source_count': int(len(group)),
        }
    return {'by_date': by_date}
