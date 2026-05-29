from __future__ import annotations

import pandas as pd

from ...core.common import DIRECT_RESOURCE_BUCKET, ELASTICITY_BUCKET, LOW_MID_HIGH, PASS_THROUGH_BUCKET, map_bucket_score, safe_text


def build_profile(stock_profile_df: pd.DataFrame, mechanism_map_df: pd.DataFrame, tuning, config):
    if stock_profile_df.empty:
        return pd.DataFrame(columns=list(stock_profile_df.columns) + ['mechanism_profile_score', 'profile_context'])
    subset = stock_profile_df.loc[stock_profile_df['mechanism_primary'] == tuning.mechanism_group].copy()
    if subset.empty:
        return subset
    weights = dict(config.get('profile_weights', {}) or {})
    subset['mechanism_profile_score'] = (
        weights.get('mapping_confidence', 0.26) * pd.to_numeric(subset['mapping_confidence'], errors='coerce').fillna(0.0)
        + weights.get('resource_exposure', 0.20) * subset['resource_exposure'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.4))
        + weights.get('elasticity_bucket', 0.18) * subset['elasticity_bucket'].map(lambda x: map_bucket_score(x, ELASTICITY_BUCKET, 0.4))
        + weights.get('direct_resource_link', 0.16) * subset['direct_resource_link'].map(lambda x: map_bucket_score(x, DIRECT_RESOURCE_BUCKET, 0.35))
        + weights.get('inventory_sensitivity', 0.12) * subset['inventory_sensitivity'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.4))
        + weights.get('cost_pass_through', 0.08) * subset['cost_pass_through'].map(lambda x: map_bucket_score(x, PASS_THROUGH_BUCKET, 0.45))
    ).round(4)
    subset['profile_score'] = (0.55 * pd.to_numeric(subset['profile_score'], errors='coerce').fillna(0.0) + 0.45 * subset['mechanism_profile_score']).round(4)
    subset['profile_context'] = subset.apply(
        lambda row: '|'.join(
            [
                safe_text(row.get('commodity_primary')),
                safe_text(row.get('direct_resource_link')),
                safe_text(row.get('elasticity_bucket')),
                safe_text(row.get('inventory_sensitivity')),
            ]
        ),
        axis=1,
    )
    return subset.sort_values(['profile_score', 'mapping_confidence', 'symbol'], ascending=[False, False, True]).reset_index(drop=True)
