from __future__ import annotations

from typing import Any, Dict, Mapping

import pandas as pd


def split_by_mechanism(signal_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if signal_df.empty:
        return {}
    return {name: group.copy() for name, group in signal_df.groupby('mechanism_primary', dropna=False)}


def select_entry_candidates(signal_df: pd.DataFrame, policy: Any, top_k: int) -> pd.DataFrame:
    if signal_df.empty:
        return signal_df.copy()
    eligible = signal_df.loc[signal_df.apply(lambda row: policy.entry_rule(row.to_dict(), context={'phase': 'backtest'}), axis=1)].copy()
    if eligible.empty:
        return eligible
    eligible = eligible.sort_values(['date', 'final_score'], ascending=[True, False])
    return eligible.groupby('date', group_keys=False).head(int(top_k)).copy()


def summarize_attribution(detail_df: pd.DataFrame) -> pd.DataFrame:
    if detail_df.empty:
        return pd.DataFrame(columns=['attribution_bucket', 'trade_count', 'avg_forward_return'])
    return (
        detail_df.groupby(['attribution_bucket', 'attribution_label'], dropna=False)['forward_return']
        .agg(['count', 'mean'])
        .reset_index()
        .rename(columns={'count': 'trade_count', 'mean': 'avg_forward_return'})
    )
