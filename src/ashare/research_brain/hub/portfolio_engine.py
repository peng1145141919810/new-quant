# -*- coding: utf-8 -*-
"""组合构建与回测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from hub.io_utils import write_csv, write_json
from hub.metrics import annualized_from_period_returns, max_drawdown_from_nav, sharpe_from_period_returns


def _load_backtest_frame(source: pd.DataFrame | str | Path, label_col: str) -> pd.DataFrame:
    wanted = {
        'date', 'code', 'ts_code', 'board', 'industry', 'listed_days', 'in_hs300',
        'is_st', 'is_suspended', 'is_limit', 'is_tradable_basic', 'close', 'pct_chg',
        'amount', 'amount_mean_20', 'vol_20', 'hs300_ret_20', label_col, 'pred_score',
    }
    if isinstance(source, pd.DataFrame):
        cols = [col for col in source.columns if col in wanted]
        return source.loc[:, cols].copy()
    path = Path(str(source))
    return pd.read_csv(path, usecols=lambda col: col in wanted)


def _apply_basic_filters(df: pd.DataFrame, strategy: Dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    if 'listed_days' in out.columns:
        out = out.loc[out['listed_days'].fillna(0) >= int(strategy.get('portfolio_min_listed_days', 120))]
    liquidity_amount = _liquidity_amount_cny(out)
    if not liquidity_amount.empty:
        out = out.assign(liquidity_amount_cny=liquidity_amount)
        out = out.loc[out['liquidity_amount_cny'].fillna(0) >= float(strategy.get('portfolio_min_amount_mean_20', 1e7))]
    if 'vol_20' in out.columns:
        out = out.loc[out['vol_20'].fillna(0) <= float(strategy.get('portfolio_max_vol_20', 0.12))]
    if 'is_st' in out.columns:
        out = out.loc[out['is_st'].fillna(0) == 0]
    return out


def _amount_to_cny(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors='coerce')
    positive = numeric.where(numeric > 0)
    return positive.where(positive >= 1_000_000, positive * 1000.0)


def _liquidity_amount_cny(df: pd.DataFrame) -> pd.Series:
    if 'amount_mean_20' not in df.columns and 'amount' not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    mean_20 = _amount_to_cny(df['amount_mean_20']) if 'amount_mean_20' in df.columns else pd.Series(index=df.index, dtype=float)
    current = _amount_to_cny(df['amount']) if 'amount' in df.columns else pd.Series(index=df.index, dtype=float)
    return mean_20.combine_first(current)


def _derive_limit_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    limit_raw = pd.to_numeric(out.get('is_limit', pd.Series(index=out.index, dtype=float)), errors='coerce').fillna(0).gt(0)
    pct = pd.to_numeric(out.get('pct_chg', pd.Series(index=out.index, dtype=float)), errors='coerce')
    out['is_limit_up'] = (limit_raw & pct.fillna(0).ge(0)).astype(int)
    out['is_limit_down'] = (limit_raw & pct.fillna(0).le(0)).astype(int)
    out['is_suspended'] = pd.to_numeric(out.get('is_suspended', pd.Series(index=out.index, dtype=float)), errors='coerce').fillna(0).astype(int)
    out['is_tradable_basic'] = pd.to_numeric(out.get('is_tradable_basic', pd.Series(index=out.index, dtype=float)), errors='coerce').fillna(0).astype(int)
    out['close'] = pd.to_numeric(out.get('close', pd.Series(index=out.index, dtype=float)), errors='coerce')
    out['pct_chg'] = pct
    return out


def _is_entry_tradable(row: pd.Series) -> bool:
    if int(row.get('is_suspended', 0) or 0) > 0:
        return False
    if int(row.get('is_limit_up', 0) or 0) > 0:
        return False
    if 'is_tradable_basic' in row and int(row.get('is_tradable_basic', 0) or 0) <= 0:
        return False
    return pd.notna(row.get('close'))


def _is_exit_tradable(row: pd.Series) -> bool:
    if int(row.get('is_suspended', 0) or 0) > 0:
        return False
    if int(row.get('is_limit_down', 0) or 0) > 0:
        return False
    return pd.notna(row.get('close'))


def _backtest_costs(strategy: Dict[str, Any]) -> Dict[str, float]:
    return {
        'buy_fee_rate': float(strategy.get('backtest_buy_fee_rate', 0.0003) or 0.0003),
        'sell_fee_rate': float(strategy.get('backtest_sell_fee_rate', 0.0003) or 0.0003),
        'sell_tax_rate': float(strategy.get('backtest_sell_tax_rate', 0.0010) or 0.0010),
        'base_slippage_bp': float(strategy.get('backtest_slippage_bp', 8.0) or 8.0),
        'queue_risk_trigger_pct': float(strategy.get('backtest_queue_risk_trigger_pct', 7.0) or 7.0),
        'queue_risk_bp': float(strategy.get('backtest_queue_risk_bp', 12.0) or 12.0),
    }


def _execution_price(close_price: float, *, side: str, slippage_bp: float) -> float:
    rate = max(float(slippage_bp or 0.0), 0.0) / 10000.0
    if side == 'buy':
        return float(close_price) * (1.0 + rate)
    return float(close_price) * (1.0 - rate)


def _queue_risk_bp(row: pd.Series, costs: Dict[str, float], *, side: str) -> float:
    pct = float(pd.to_numeric(row.get('pct_chg'), errors='coerce') or 0.0)
    trigger = abs(float(costs.get('queue_risk_trigger_pct', 7.0) or 7.0))
    extra_bp = float(costs.get('queue_risk_bp', 12.0) or 12.0)
    if side == 'buy' and pct >= trigger:
        return extra_bp
    if side == 'sell' and pct <= -trigger:
        return extra_bp
    return 0.0


def build_latest_portfolio(latest_scores_df: pd.DataFrame, strategy: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    df = _apply_basic_filters(latest_scores_df, strategy)
    top_k = int(strategy.get('top_k', 20))
    df = df.sort_values('pred_score', ascending=False).head(top_k).copy()

    weak = False
    if 'hs300_ret_20' in df.columns and df['hs300_ret_20'].notna().any():
        weak = bool(float(df['hs300_ret_20'].iloc[0]) < 0)
    exposure = float(strategy.get('portfolio_weak_market_exposure', 0.5) if weak else strategy.get('portfolio_base_exposure', 1.0))
    n = max(len(df), 1)
    df['portfolio_weight'] = exposure / n
    df['target_exposure'] = exposure
    df['cash_buffer'] = 1.0 - exposure

    cols = [c for c in ['date', 'code', 'ts_code', 'board', 'industry', 'close', 'pred_score', 'portfolio_weight', 'target_exposure', 'cash_buffer', 'liquidity_amount_cny', 'amount', 'amount_mean_20', 'vol_20', 'listed_days', 'in_hs300'] if c in df.columns]
    latest_portfolio_path = out_dir / 'latest_portfolio_v1.csv'
    write_csv(latest_portfolio_path, df[cols])
    return {
        'latest_portfolio_path': str(latest_portfolio_path),
        'n_pick': int(len(df)),
        'target_exposure': exposure,
        'market_is_weak': weak,
        'latest_portfolio_df': df,
    }


def backtest_from_pred_test(pred_test_df: pd.DataFrame | str | Path, strategy: Dict[str, Any], out_dir: Path, label_col: str) -> Dict[str, Any]:
    df = _load_backtest_frame(pred_test_df, label_col=label_col)
    if 'date' not in df.columns:
        raise ValueError('pred_test_df 缺少 date 列，无法回测')
    df['date'] = pd.to_datetime(df['date'])
    df = _derive_limit_flags(df)
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    dates = sorted(df['date'].dropna().unique().tolist())
    horizon = int(strategy.get('portfolio_holding_days', 5) or 5)
    top_k = int(strategy.get('top_k', 20) or 20)
    costs = _backtest_costs(strategy)

    curve_rows: List[Dict[str, Any]] = []
    holding_rows: List[Dict[str, Any]] = []
    nav = 1.0
    peak = 1.0

    code_groups = {str(code): g.reset_index(drop=True) for code, g in df.groupby('code', sort=False)}

    for dt in dates[::max(horizon, 1)]:
        signal_universe = df.loc[df['date'] == dt].copy()
        signal_universe = _apply_basic_filters(signal_universe, strategy)
        if signal_universe.empty:
            continue
        signal_universe = signal_universe.sort_values('pred_score', ascending=False).head(top_k).copy()

        weak = False
        if 'hs300_ret_20' in signal_universe.columns and signal_universe['hs300_ret_20'].notna().any():
            weak = bool(float(signal_universe['hs300_ret_20'].iloc[0]) < 0)
        exposure = float(strategy.get('portfolio_weak_market_exposure', 0.5) if weak else strategy.get('portfolio_base_exposure', 1.0))

        realized_rets: List[float] = []
        executed_names = 0
        skipped_names = 0
        period_holding_rows: List[Dict[str, Any]] = []

        for _, row in signal_universe.iterrows():
            code = str(row.get('code', '') or '')
            code_df = code_groups.get(code)
            if code_df is None or code_df.empty:
                skipped_names += 1
                continue

            match_idx = code_df.index[code_df['date'] == dt]
            if len(match_idx) == 0:
                skipped_names += 1
                continue
            signal_idx = int(match_idx[0])
            entry_idx = signal_idx + 1
            if entry_idx >= len(code_df.index):
                period_holding_rows.append({
                    'signal_date': dt,
                    'entry_date': pd.NaT,
                    'exit_date': pd.NaT,
                    'code': code,
                    'industry': row.get('industry'),
                    'board': row.get('board'),
                    'pred_score': row.get('pred_score'),
                    'status': 'skip_no_next_bar',
                    'skip_reason': 'no_next_bar',
                })
                skipped_names += 1
                continue

            entry_row = code_df.iloc[entry_idx]
            if not _is_entry_tradable(entry_row):
                period_holding_rows.append({
                    'signal_date': dt,
                    'entry_date': entry_row.get('date'),
                    'exit_date': pd.NaT,
                    'code': code,
                    'industry': row.get('industry'),
                    'board': row.get('board'),
                    'pred_score': row.get('pred_score'),
                    'status': 'skip_untradable_entry',
                    'skip_reason': 'entry_suspended_or_limit_up_or_not_basic',
                    'entry_close': entry_row.get('close'),
                    'entry_is_suspended': entry_row.get('is_suspended'),
                    'entry_is_limit_up': entry_row.get('is_limit_up'),
                    'entry_is_tradable_basic': entry_row.get('is_tradable_basic'),
                })
                skipped_names += 1
                continue

            target_exit_idx = entry_idx + max(horizon, 1)
            if target_exit_idx >= len(code_df.index):
                period_holding_rows.append({
                    'signal_date': dt,
                    'entry_date': entry_row.get('date'),
                    'exit_date': pd.NaT,
                    'code': code,
                    'industry': row.get('industry'),
                    'board': row.get('board'),
                    'pred_score': row.get('pred_score'),
                    'status': 'skip_incomplete_horizon',
                    'skip_reason': 'not_enough_future_bars',
                    'entry_close': entry_row.get('close'),
                })
                skipped_names += 1
                continue

            exit_idx = None
            for probe_idx in range(target_exit_idx, len(code_df.index)):
                probe_row = code_df.iloc[probe_idx]
                if _is_exit_tradable(probe_row):
                    exit_idx = probe_idx
                    break
            if exit_idx is None:
                period_holding_rows.append({
                    'signal_date': dt,
                    'entry_date': entry_row.get('date'),
                    'exit_date': pd.NaT,
                    'code': code,
                    'industry': row.get('industry'),
                    'board': row.get('board'),
                    'pred_score': row.get('pred_score'),
                    'status': 'skip_untradable_exit',
                    'skip_reason': 'no_sellable_bar_after_horizon',
                    'entry_close': entry_row.get('close'),
                })
                skipped_names += 1
                continue

            exit_row = code_df.iloc[exit_idx]
            entry_close = float(entry_row['close'])
            exit_close = float(exit_row['close'])
            entry_queue_bp = _queue_risk_bp(entry_row, costs, side='buy')
            exit_queue_bp = _queue_risk_bp(exit_row, costs, side='sell')
            entry_exec_price = _execution_price(
                entry_close,
                side='buy',
                slippage_bp=float(costs['base_slippage_bp']) + float(entry_queue_bp),
            )
            exit_exec_price = _execution_price(
                exit_close,
                side='sell',
                slippage_bp=float(costs['base_slippage_bp']) + float(exit_queue_bp),
            )
            gross_realized_ret = exit_exec_price / entry_exec_price - 1.0
            realized_ret = gross_realized_ret - float(costs['buy_fee_rate']) - float(costs['sell_fee_rate']) - float(costs['sell_tax_rate'])
            realized_rets.append(realized_ret)
            executed_names += 1

            period_holding_rows.append({
                'signal_date': dt,
                'entry_date': entry_row.get('date'),
                'exit_date': exit_row.get('date'),
                'code': code,
                'industry': row.get('industry'),
                'board': row.get('board'),
                'pred_score': row.get('pred_score'),
                'status': 'executed',
                'skip_reason': '',
                'weight': np.nan,
                'signal_close': row.get('close'),
                'entry_close': entry_close,
                'exit_close': exit_close,
                'entry_exec_price': entry_exec_price,
                'exit_exec_price': exit_exec_price,
                'raw_label_ret': row.get(label_col),
                'gross_realized_ret': gross_realized_ret,
                'realized_ret': realized_ret,
                'holding_bars_from_entry': int(exit_idx - entry_idx),
                'buy_fee_rate': costs['buy_fee_rate'],
                'sell_fee_rate': costs['sell_fee_rate'],
                'sell_tax_rate': costs['sell_tax_rate'],
                'base_slippage_bp': costs['base_slippage_bp'],
                'entry_queue_risk_bp': entry_queue_bp,
                'exit_queue_risk_bp': exit_queue_bp,
                'entry_is_suspended': entry_row.get('is_suspended'),
                'entry_is_limit_up': entry_row.get('is_limit_up'),
                'exit_is_suspended': exit_row.get('is_suspended'),
                'exit_is_limit_down': exit_row.get('is_limit_down'),
            })

        per_name_weight = exposure / max(executed_names, 1)
        for item in period_holding_rows:
            if item.get('status') == 'executed':
                item['weight'] = per_name_weight
                item['cash_weight'] = 1.0 - exposure
                item['target_exposure'] = exposure
                item['weak_market'] = int(weak)
        holding_rows.extend(period_holding_rows)

        period_ret = float(np.mean(realized_rets)) * exposure if realized_rets else 0.0
        nav *= (1.0 + period_ret)
        peak = max(peak, nav)
        drawdown = nav / peak - 1.0
        curve_rows.append({
            'signal_date': dt,
            'n_signal': int(len(signal_universe)),
            'n_executed': int(executed_names),
            'n_skipped': int(skipped_names),
            'period_ret': period_ret,
            'target_exposure': exposure,
            'market_is_weak': int(weak),
            'nav': nav,
            'drawdown': drawdown,
            'execution_model': 'next_bar_close_cost_aware_limit_aware_exit',
        })

    curve = pd.DataFrame(curve_rows)
    holdings = pd.DataFrame(holding_rows)
    write_csv(out_dir / 'portfolio_backtest_curve.csv', curve)
    write_csv(out_dir / 'portfolio_backtest_holdings.csv', holdings)

    annualized = annualized_from_period_returns(curve['period_ret'].tolist() if not curve.empty else [], periods_per_year=max(240 // max(horizon, 1), 1))
    sharpe = sharpe_from_period_returns(curve['period_ret'].tolist() if not curve.empty else [], periods_per_year=max(240 // max(horizon, 1), 1))
    mdd = max_drawdown_from_nav(curve['nav'].tolist() if not curve.empty else [1.0])
    payload = {
        'annualized_ret': float(annualized),
        'sharpe': float(sharpe),
        'max_drawdown': float(mdd),
        'n_rebalance': int(len(curve)),
        'curve_path': str(out_dir / 'portfolio_backtest_curve.csv'),
        'holdings_path': str(out_dir / 'portfolio_backtest_holdings.csv'),
        'execution_model': 'next_bar_close_cost_aware_limit_aware_exit',
        'backtest_costs': costs,
    }
    write_json(out_dir / 'portfolio_summary.json', payload)
    return payload
