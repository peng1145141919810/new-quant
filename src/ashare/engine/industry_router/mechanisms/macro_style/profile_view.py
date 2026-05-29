from __future__ import annotations

import pandas as pd

from ...core.common import DEFENSIVE_BUCKET, LOW_MID_HIGH, STYLE_BUCKET, map_bucket_score, safe_text


def build_profile(stock_profile_df: pd.DataFrame, mechanism_map_df: pd.DataFrame, tuning, config):
    if stock_profile_df.empty:
        return pd.DataFrame(columns=list(stock_profile_df.columns) + ['mechanism_profile_score', 'profile_context'])
    subset = stock_profile_df.loc[stock_profile_df['mechanism_primary'] == tuning.mechanism_group].copy()
    if subset.empty:
        return subset
    weights = dict(config.get('profile_weights', {}) or {})
    subset['mechanism_profile_score'] = (
        weights.get('mapping_confidence', 0.22) * pd.to_numeric(subset['mapping_confidence'], errors='coerce').fillna(0.0)
        + weights.get('style_bucket', 0.22) * subset['style_bucket'].map(lambda x: map_bucket_score(x, STYLE_BUCKET, 0.45))
        + weights.get('duration_sensitivity', 0.16) * subset['duration_sensitivity'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.45))
        + weights.get('yield_sensitivity', 0.14) * subset['yield_sensitivity'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.45))
        + weights.get('macro_beta_bucket', 0.12) * subset['macro_beta_bucket'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.45))
        + weights.get('defensive_vs_offensive', 0.14) * subset['defensive_vs_offensive'].map(lambda x: map_bucket_score(x, DEFENSIVE_BUCKET, 0.5))
    ).round(4)
    subset['profile_score'] = (0.55 * pd.to_numeric(subset['profile_score'], errors='coerce').fillna(0.0) + 0.45 * subset['mechanism_profile_score']).round(4)
    subset['profile_context'] = subset.apply(
        lambda row: '|'.join(
            [
                safe_text(row.get('style_bucket')),
                safe_text(row.get('duration_sensitivity')),
                safe_text(row.get('yield_sensitivity')),
                safe_text(row.get('defensive_vs_offensive')),
            ]
        ),
        axis=1,
    )
    return subset.sort_values(['profile_score', 'mapping_confidence', 'symbol'], ascending=[False, False, True]).reset_index(drop=True)
