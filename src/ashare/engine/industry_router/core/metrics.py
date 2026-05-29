from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


def max_drawdown(daily_returns: List[float]) -> float:
    if not daily_returns:
        return 0.0
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for ret in daily_returns:
        equity *= 1.0 + float(ret)
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        worst = min(worst, drawdown)
    return round(worst, 6)


def summarize_horizon(details_df: pd.DataFrame) -> Dict[str, Any]:
    if details_df.empty:
        return {
            'trade_count': 0,
            'signal_days': 0,
            'avg_forward_return': 0.0,
            'win_rate': 0.0,
            'cumulative_return': 0.0,
            'max_drawdown': 0.0,
            'avg_final_score': 0.0,
        }
    day_returns = details_df.groupby('signal_date')['forward_return'].mean().tolist()
    cumulative = 1.0
    for value in day_returns:
        cumulative *= 1.0 + float(value)
    return {
        'trade_count': int(len(details_df)),
        'signal_days': int(details_df['signal_date'].nunique()),
        'avg_forward_return': round(float(details_df['forward_return'].mean()), 6),
        'win_rate': round(float((details_df['forward_return'] > 0).mean()), 6),
        'cumulative_return': round(cumulative - 1.0, 6),
        'max_drawdown': max_drawdown(day_returns),
        'avg_final_score': round(float(details_df['final_score'].mean()), 6),
    }
