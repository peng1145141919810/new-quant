# -*- coding: utf-8 -*-
"""组合构建与回测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from hub.io_utils import write_csv, write_json
from hub.metrics import annualized_from_period_returns, max_drawdown_from_nav, sharpe_from_period_returns


def _load_backtest_frame(source: pd.DataFrame | str | Path, label_col: str) -> pd.DataFrame:
    wanted = {
        'date', 'code', 'ts_code', 'board', 'industry', 'listed_days', 'in_hs300',
        'is_st', 'is_suspended', 'is_limit', 'is_tradable_basic', 'close', 'close_raw', 'pct_chg',
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
    # P5 集中模式流动性保护：低价票冲击成本高、易踩坑；默认 0=关，赚钱模式可收紧。
    # 价格地板比的是"当时真实成交价"，必须用未复权 close_raw；qfq 后的 close 在历史段
    # 被复权比例缩放，会把当年真实价格远高于地板的老观测误删。close_raw 缺失才退回 close。
    min_price = float(strategy.get('portfolio_min_price', 0.0) or 0.0)
    price_col = 'close_raw' if 'close_raw' in out.columns else ('close' if 'close' in out.columns else '')
    if min_price > 0 and price_col:
        out = out.loc[pd.to_numeric(out[price_col], errors='coerce').fillna(0) >= min_price]
    # 板块排除：可排掉北交所(BSE)这类流动性极差的板块；默认空=不排。
    exclude_boards = strategy.get('portfolio_exclude_boards', []) or []
    if exclude_boards and 'board' in out.columns:
        _excl = {str(b).strip().upper() for b in exclude_boards}
        out = out.loc[~out['board'].astype(str).str.strip().str.upper().isin(_excl)]
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


def _cap_weights(weights: np.ndarray, cap_abs: float, *, iters: int = 64) -> np.ndarray:
    """单票绝对上限：把超额权重按比例再分给未触顶的票，迭代到收敛。

    超出 cap 的部分若无处可放（全部触顶），则留作现金（总和会 < 原 exposure）。
    """
    w = np.asarray(weights, dtype=float).copy()
    if cap_abs <= 0 or w.sum() <= 0:
        return w
    for _ in range(int(iters)):
        over = w > cap_abs + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap_abs).sum())
        w[over] = cap_abs
        under = ~over
        room = (cap_abs - w[under]).clip(min=0.0)
        room_sum = float(room.sum())
        if room_sum <= 1e-12 or excess <= 1e-12:
            break
        w[under] += excess * (room / room_sum)
    return np.minimum(w, cap_abs)


def _cap_industry_weights(weights: np.ndarray, industries: np.ndarray, cap_abs: float, *, iters: int = 32) -> np.ndarray:
    """单行业绝对上限：缩减超限行业内各票，多出的权重再分给未超限行业的票。"""
    w = np.asarray(weights, dtype=float).copy()
    inds = np.asarray(industries, dtype=object)
    if cap_abs <= 0 or w.sum() <= 0 or len(inds) != len(w):
        return w
    for _ in range(int(iters)):
        moved = 0.0
        ind_totals: Dict[Any, float] = {}
        for ind, wi in zip(inds, w):
            ind_totals[ind] = ind_totals.get(ind, 0.0) + float(wi)
        over_inds = {ind for ind, tot in ind_totals.items() if tot > cap_abs + 1e-12}
        if not over_inds:
            break
        for ind in over_inds:
            mask = np.array([x == ind for x in inds])
            tot = float(w[mask].sum())
            if tot <= 0:
                continue
            scale = cap_abs / tot
            moved += tot - cap_abs
            w[mask] = w[mask] * scale
        # 把腾出的权重按当前权重比例分给“未超限行业”的票
        under_mask = np.array([x not in over_inds for x in inds])
        base = w[under_mask]
        base_sum = float(base.sum())
        if moved <= 1e-12 or base_sum <= 1e-12:
            break
        w[under_mask] += moved * (base / base_sum)
    return w


def _limit_names_per_industry(df: pd.DataFrame, max_per_industry: int) -> pd.DataFrame:
    """每个行业最多保留 max_per_industry 只。

    入参 df 需已按 pred_score 降序排好；按行顺序贪心保留（高分票优先），
    某行业满额后其余同业票被丢弃，名额让给后面别的行业的票。
    industry 缺失（NaN/None）的票不受约束，避免行业数据缺失时被误删。
    这是“数量上限”，与 _cap_industry_weights 的“权重上限”互补：
    前者控制一个行业能进几只，后者控制进来后总权重不超限。
    """
    if max_per_industry <= 0 or 'industry' not in df.columns or df.empty:
        return df
    counts: Dict[Any, int] = {}
    keep_mask: List[bool] = []
    for ind in df['industry'].tolist():
        if pd.isna(ind):
            keep_mask.append(True)
            continue
        c = counts.get(ind, 0)
        if c < max_per_industry:
            counts[ind] = c + 1
            keep_mask.append(True)
        else:
            keep_mask.append(False)
    return df.loc[keep_mask]


def _signal_weights(
    pred_scores: np.ndarray,
    exposure: float,
    strategy: Dict[str, Any],
    industries: Optional[np.ndarray] = None,
    vols: Optional[np.ndarray] = None,
) -> np.ndarray:
    """按预测分给持仓分配权重（score 倾斜），并施加单票/行业上限。

    pred_scores 需已按降序排列（第 1 名打分最高）。
    portfolio_weight_scheme:
      - 'equal'      : 均权（旧行为，回退用）
      - 'score_tilt' : 按 pred_score 相对强度倾斜（默认，保留“信心”信息）
      - 'rank_tilt'  : 按名次倾斜（忽略分差，对异常分更稳健）
    portfolio_weight_power: 倾斜强度，0=均权，越大越向头部集中。默认 1.5。
    portfolio_single_name_cap / portfolio_single_industry_cap: 单票/单行业绝对上限。
    """
    n = int(len(pred_scores))
    if n == 0:
        return np.zeros(0, dtype=float)
    exposure = float(max(exposure, 0.0))
    scheme = str(strategy.get('portfolio_weight_scheme', 'score_tilt') or 'score_tilt').lower()
    power = float(strategy.get('portfolio_weight_power', 1.5) or 0.0)

    if n == 1 or scheme == 'equal' or power <= 0:
        raw = np.ones(n, dtype=float)
    elif scheme == 'rank_tilt':
        ranks = np.arange(n, 0, -1, dtype=float)  # n, n-1, ..., 1
        raw = ranks ** power
    else:  # score_tilt
        s = np.asarray(pred_scores, dtype=float)
        finite = np.isfinite(s)
        s_min = float(np.nanmin(np.where(finite, s, np.nan))) if finite.any() else 0.0
        s_max = float(np.nanmax(np.where(finite, s, np.nan))) if finite.any() else 0.0
        span = s_max - s_min
        floor = span * 0.1 if span > 0 else 1.0  # 给最低分留底仓，避免 0 权重
        shifted = np.where(finite, s - s_min + floor, floor)
        raw = np.asarray(shifted, dtype=float) ** power

    raw = np.where(np.isfinite(raw) & (raw > 0), raw, 0.0)
    if raw.sum() <= 0:
        raw = np.ones(n, dtype=float)
    weights = raw / raw.sum() * exposure

    # 波动刹车：按 20 日波动率反向缩放权重，压低高波动票的仓位、抬高低波动票。
    # momentum 类组合专挑强动量高波动票、易因子踩踏，这一脚刹车主要救它；
    # high_drawdown 是全局病灶，所以对所有候选都生效。power=0 时关闭（旧行为）。
    vol_tilt = float(strategy.get('portfolio_inverse_vol_tilt_power', 0.0) or 0.0)
    if vol_tilt > 0 and vols is not None and len(vols) == n:
        v = np.asarray(vols, dtype=float)
        finite = np.isfinite(v) & (v > 0)
        if finite.any():
            med = float(np.median(v[finite]))
            if med > 0:
                factor = np.where(finite, (med / np.maximum(v, 1e-9)) ** vol_tilt, 1.0)
                factor = np.clip(factor, 0.25, 4.0)  # 限幅：避免极端波动把权重打到 0 或爆表
                weights = weights * factor
                s = float(weights.sum())
                if s > 0:
                    weights = weights / s * exposure

    name_cap = float(strategy.get('portfolio_single_name_cap', 0.0) or 0.0)
    if name_cap > 0:
        weights = _cap_weights(weights, name_cap)
    ind_cap = float(strategy.get('portfolio_single_industry_cap', 0.0) or 0.0)
    if ind_cap > 0 and industries is not None and len(industries) == n:
        weights = _cap_industry_weights(weights, np.asarray(industries), ind_cap)
        if name_cap > 0:
            weights = _cap_weights(weights, name_cap)  # 行业再分配后复查单票上限
    return weights


def build_latest_portfolio(latest_scores_df: pd.DataFrame, strategy: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    df = _apply_basic_filters(latest_scores_df, strategy)
    top_k = int(strategy.get('top_k', 20))
    # 先按行业数量上限截断，再取 top_k：超额行业的低分票被剔除，名额让给别的行业，
    # 否则单行业内连排好几名会把 top_k 名额吃掉，组合行业过度集中。
    max_per_ind = int(strategy.get('portfolio_max_names_per_industry', 0) or 0)
    df = df.sort_values('pred_score', ascending=False)
    df = _limit_names_per_industry(df, max_per_ind)
    df = df.head(top_k).copy()

    weak = False
    if 'hs300_ret_20' in df.columns and df['hs300_ret_20'].notna().any():
        weak = bool(float(df['hs300_ret_20'].iloc[0]) < 0)
    exposure = float(strategy.get('portfolio_weak_market_exposure', 0.5) if weak else strategy.get('portfolio_base_exposure', 1.0))
    industries = df['industry'].to_numpy() if 'industry' in df.columns else None
    vols = df['vol_20'].to_numpy() if 'vol_20' in df.columns else None
    weights = _signal_weights(df['pred_score'].to_numpy(), exposure, strategy, industries=industries, vols=vols)
    df['portfolio_weight'] = weights
    df['target_exposure'] = exposure
    df['cash_buffer'] = 1.0 - float(np.sum(weights))

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
        # P6 回测补位：不预砍 top_k，留更大候选池，循环里凑满 top_k 只“可成交”的票才停。
        # 涨停/停牌买不进的票被 skip 后用下一名替补，避免 survivor 过度集中 + 产量过低。
        signal_universe = signal_universe.sort_values('pred_score', ascending=False)
        _fill_pool = int(strategy.get('portfolio_backtest_fill_pool', 0) or 0)
        if _fill_pool <= 0:
            _fill_pool = top_k * 4
        signal_universe = signal_universe.head(max(_fill_pool, top_k)).copy()

        weak = False
        if 'hs300_ret_20' in signal_universe.columns and signal_universe['hs300_ret_20'].notna().any():
            weak = bool(float(signal_universe['hs300_ret_20'].iloc[0]) < 0)
        exposure = float(strategy.get('portfolio_weak_market_exposure', 0.5) if weak else strategy.get('portfolio_base_exposure', 1.0))

        realized_rets: List[float] = []
        executed_scores: List[float] = []
        executed_industries: List[Any] = []
        executed_vols: List[float] = []
        executed_names = 0
        skipped_names = 0
        period_holding_rows: List[Dict[str, Any]] = []
        # 行业数量上限：与实盘 build_latest_portfolio 同一口径，回测才能反映真实约束。
        max_per_ind = int(strategy.get('portfolio_max_names_per_industry', 0) or 0)
        industry_exec_counts: Dict[Any, int] = {}

        for _, row in signal_universe.iterrows():
            code = str(row.get('code', '') or '')
            ind = row.get('industry')
            # 行业已满额则直接跳过（纯属没选它，不计入 skip 统计，避免污染 skip 率）。
            if max_per_ind > 0 and not pd.isna(ind) and industry_exec_counts.get(ind, 0) >= max_per_ind:
                continue
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
            executed_scores.append(float(pd.to_numeric(row.get('pred_score'), errors='coerce')))
            executed_industries.append(row.get('industry'))
            executed_vols.append(pd.to_numeric(row.get('vol_20'), errors='coerce'))
            executed_names += 1
            if not pd.isna(ind):
                industry_exec_counts[ind] = industry_exec_counts.get(ind, 0) + 1

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

            # P6 凑满 top_k 只可成交票即停，跳过的涨停/停牌已被上面替补。
            if executed_names >= top_k:
                break

        # 与实盘 build_latest_portfolio 共用同一套 score 倾斜权重，回测才能反映集中下注
        if executed_names > 0:
            period_weights = _signal_weights(
                np.asarray(executed_scores, dtype=float),
                exposure,
                strategy,
                industries=np.asarray(executed_industries, dtype=object),
                vols=np.asarray(executed_vols, dtype=float),
            )
        else:
            period_weights = np.zeros(0, dtype=float)
        _wi = 0
        for item in period_holding_rows:
            if item.get('status') == 'executed':
                item['weight'] = float(period_weights[_wi])
                item['cash_weight'] = 1.0 - float(np.sum(period_weights))
                item['target_exposure'] = exposure
                item['weak_market'] = int(weak)
                _wi += 1
        holding_rows.extend(period_holding_rows)

        # 组合期收益 = Σ(权重 × 个股实现收益)；权重和≤exposure（cap 超额自动留现金）。
        # scheme=equal 时退化为 mean(realized)×exposure，与旧逻辑一致。
        period_ret = float(np.dot(period_weights, np.asarray(realized_rets, dtype=float))) if executed_names > 0 else 0.0
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
