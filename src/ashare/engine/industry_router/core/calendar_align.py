from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .common import normalize_symbol, safe_float, safe_text


def load_price_frame(price_root: Path, symbol: str, cache: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    key = normalize_symbol(symbol)
    if key in cache:
        return cache[key]
    code = key.split('.', 1)[0] if '.' in key else key
    path = price_root / f'{code}.csv'
    cols = ['date', 'open', 'close', 'high', 'low', 'amount', 'pct_chg', 'volume_ratio']
    if not path.exists():
        cache[key] = pd.DataFrame(columns=cols)
        return cache[key]
    try:
        df = pd.read_csv(path, usecols=lambda c: c in cols)
    except Exception:
        cache[key] = pd.DataFrame(columns=cols)
        return cache[key]
    if df.empty:
        cache[key] = pd.DataFrame(columns=cols)
        return cache[key]
    for col in cols:
        if col not in df.columns:
            df[col] = None
    df = df.dropna(subset=['date', 'close']).copy()
    df['date'] = df['date'].astype(str).str.slice(0, 10)
    for col in ['open', 'close', 'high', 'low', 'amount', 'pct_chg', 'volume_ratio']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)
    cache[key] = df
    return df


def find_trade_index(price_df: pd.DataFrame, trade_date: str) -> int | None:
    if price_df.empty:
        return None
    dates = price_df['date'].astype(str).tolist()
    for idx, item in enumerate(dates):
        if item >= safe_text(trade_date):
            return idx
    return None


def history_slice(price_df: pd.DataFrame, idx: int, lookback: int) -> pd.DataFrame:
    start = max(0, int(idx) - int(lookback))
    return price_df.iloc[start:idx].copy()


def forward_slice(price_df: pd.DataFrame, idx: int, horizon: int) -> pd.DataFrame:
    end = min(len(price_df), int(idx) + int(horizon) + 1)
    return price_df.iloc[idx:end].copy()


def compute_price_feature_snapshot(price_df: pd.DataFrame, trade_date: str) -> Dict[str, float]:
    idx = find_trade_index(price_df, trade_date)
    if idx is None:
        return {
            'pre_3d_return': 0.0,
            'pre_5d_return': 0.0,
            'pre_10d_return': 0.0,
            'amount_ratio_5d': 1.0,
            'volume_ratio': 1.0,
            'pct_chg': 0.0,
            'drawup_10d': 0.0,
        }
    row = price_df.iloc[idx]
    close_now = safe_float(row.get('close'), 0.0)
    amount_now = safe_float(row.get('amount'), 0.0)
    hist_3 = history_slice(price_df, idx, 3)
    hist_5 = history_slice(price_df, idx, 5)
    hist_10 = history_slice(price_df, idx, 10)

    def _return_from(hist_df: pd.DataFrame) -> float:
        if hist_df.empty or close_now <= 0:
            return 0.0
        base = safe_float(hist_df.iloc[0].get('close'), 0.0)
        if base <= 0:
            return 0.0
        return round(close_now / base - 1.0, 6)

    mean_amount_5 = float(pd.to_numeric(hist_5['amount'], errors='coerce').dropna().mean()) if not hist_5.empty else 0.0
    high_10 = float(pd.to_numeric(hist_10['high'], errors='coerce').dropna().max()) if not hist_10.empty else close_now
    drawup_10d = 0.0 if close_now <= 0 else round((high_10 / close_now - 1.0), 6)
    return {
        'pre_3d_return': _return_from(hist_3),
        'pre_5d_return': _return_from(hist_5),
        'pre_10d_return': _return_from(hist_10),
        'amount_ratio_5d': round(amount_now / mean_amount_5, 6) if mean_amount_5 > 0 else 1.0,
        'volume_ratio': safe_float(row.get('volume_ratio'), 1.0) or 1.0,
        'pct_chg': safe_float(row.get('pct_chg'), 0.0) / 100.0 if abs(safe_float(row.get('pct_chg'), 0.0)) > 1 else safe_float(row.get('pct_chg'), 0.0),
        'drawup_10d': drawup_10d,
    }


def basket_relative_strength(price_root: Path, symbols: List[str], trade_date: str, cache: Dict[str, pd.DataFrame], lookback: int = 5) -> float:
    returns: List[float] = []
    for symbol in symbols:
        price_df = load_price_frame(price_root=price_root, symbol=symbol, cache=cache)
        snap = compute_price_feature_snapshot(price_df=price_df, trade_date=trade_date)
        returns.append(safe_float(snap.get(f'pre_{lookback}d_return'), 0.0))
    if not returns:
        return 0.0
    return round(sum(returns) / max(len(returns), 1), 6)
