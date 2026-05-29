from __future__ import annotations

import pandas as pd

from ...core.common import BENEFIT_MODE_BUCKET, CUSTOMER_ANCHOR_BUCKET, GLOBAL_EXPOSURE_BUCKET, LOW_MID_HIGH, map_bucket_score, safe_text


def build_profile(stock_profile_df: pd.DataFrame, mechanism_map_df: pd.DataFrame, tuning, config):
    if stock_profile_df.empty:
        return pd.DataFrame(columns=list(stock_profile_df.columns) + ['mechanism_profile_score', 'profile_context'])
    subset = stock_profile_df.loc[stock_profile_df['mechanism_primary'] == tuning.mechanism_group].copy()
    if subset.empty:
        return subset
    weights = dict(config.get('profile_weights', {}) or {})
    subset['mechanism_profile_score'] = (
        weights.get('mapping_confidence', 0.34) * pd.to_numeric(subset['mapping_confidence'], errors='coerce').fillna(0.0)
        + weights.get('benefit_mode', 0.24) * subset['benefit_mode'].map(lambda x: map_bucket_score(x, BENEFIT_MODE_BUCKET, 0.35))
        + weights.get('spec_upgrade_level', 0.16) * subset['spec_upgrade_level'].map(lambda x: map_bucket_score(x, LOW_MID_HIGH, 0.4))
        + weights.get('customer_anchor', 0.14) * subset['customer_anchor'].map(lambda x: map_bucket_score(x, CUSTOMER_ANCHOR_BUCKET, 0.35))
        + weights.get('global_exposure', 0.12) * subset['global_vs_domestic_exposure'].map(lambda x: map_bucket_score(x, GLOBAL_EXPOSURE_BUCKET, 0.4))
    ).round(4)
    subset['profile_score'] = (0.55 * pd.to_numeric(subset['profile_score'], errors='coerce').fillna(0.0) + 0.45 * subset['mechanism_profile_score']).round(4)
    subset['profile_context'] = subset.apply(
        lambda row: '|'.join(
            [
                safe_text(row.get('subchain_primary')),
                safe_text(row.get('benefit_mode')),
                safe_text(row.get('customer_anchor')),
                safe_text(row.get('global_vs_domestic_exposure')),
            ]
        ),
        axis=1,
    )
    return subset.sort_values(['profile_score', 'mapping_confidence', 'symbol'], ascending=[False, False, True]).reset_index(drop=True)
