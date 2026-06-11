# -*- coding: utf-8 -*-
"""V6 持仓建议层：读取 V5.1 最优实验输出，生成模拟盘前建议。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

from .candidate_pipeline import (
    activate_and_rank_candidate_pool,
    apply_candidate_llm_overlay,
    choose_candidate_pool,
    sort_candidate_pool,
    summarize_candidate_pool,
)
from .config_utils import ensure_dir
from .integrated_thesis import load_latest_integrated_thesis_state
from .market_state import load_latest_market_state
from .alpha_attribution import build_alpha_attribution
from .alpha_registry import apply_registered_family_budget
from .alpha_lifecycle import apply_alpha_lifecycle_weight_bias, build_alpha_lifecycle
from .portfolio_construction_pipeline import (
    account_allocator_profile,
    account_size_adjusted_limits,
    clean_target_position_columns,
    diversify_portfolio_weights,
    enforce_min_executable_weights,
    rebalance_to_target_fill,
    select_account_aware_candidates,
)
from .llm_operating_brain import build_operating_brain
from .evidence_audit import apply_evidence_gate
from .portfolio_pre_release_objective import apply_pre_release_proxy_objective
from .decision import decide_target_weights, load_decision_constraints
from .portfolio import build_portfolio_artifacts
from .technical_confirmation import build_technical_confirmation_artifacts


def _series_or_zero(df: pd.DataFrame, col: str) -> pd.Series:
    """安全获取 DataFrame 中可能缺失的列；缺失或为标量时返回全 0 同长度 Series。

    场景：V5 latest_portfolio 上游可能没有跑过 strategy_activation 富化，导致
    valuation_signal_score / liquidity_signal_score / seed_weight_norm 等列缺失。
    `pd.to_numeric(df.get("missing"), errors="coerce")` 会返回标量 nan，
    后续 .fillna(0.0) 在 numpy.float64 上会抛 AttributeError。
    """
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return pd.Series(0.0, index=df.index, dtype="float64")


def _sort_score_frame(df: pd.DataFrame) -> pd.DataFrame:
    sort_cols = [c for c in ['total_score', 'sharpe', 'valid_ic', 'created_at'] if c in df.columns]
    if not sort_cols:
        return df
    return df.sort_values(sort_cols, ascending=[False] * len(sort_cols))


def _resolve_run_dir(hub_root: Path, row: Dict[str, Any]) -> Path | None:
    candidates: List[Path] = []
    run_id = str(row.get('run_id', '') or '').strip()
    if run_id:
        candidates.append(hub_root / 'runs' / run_id)
    for key in ['latest_portfolio_path', 'portfolio_summary_path', 'train_summary_path', 'pred_test_path']:
        raw = str(row.get(key, '') or '').strip()
        if not raw:
            continue
        path = Path(raw)
        candidates.append(path.parent if path.suffix else path)
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        if candidate.exists() and (candidate / 'latest_portfolio_v1.csv').exists():
            return candidate
    return None


def _latest_cycle_results(hub_root: Path) -> pd.DataFrame:
    state_path = hub_root / 'controller_state.json'
    if not state_path.exists():
        return pd.DataFrame()
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:
        return pd.DataFrame()
    cycle_id = str(state.get('last_cycle_id', '') or '').strip()
    if not cycle_id:
        return pd.DataFrame()
    cycle_summary_path = hub_root / 'cycles' / cycle_id / 'cycle_summary.json'
    if not cycle_summary_path.exists():
        return pd.DataFrame()
    try:
        payload = json.loads(cycle_summary_path.read_text(encoding='utf-8'))
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame(list(payload.get('results', []) or []))


def _latest_cycle_gate(hub_root: Path) -> Dict[str, Any]:
    state_path = hub_root / 'controller_state.json'
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    cycle_id = str(state.get('last_cycle_id', '') or '').strip()
    if not cycle_id:
        return {}
    cycle_summary_path = hub_root / 'cycles' / cycle_id / 'cycle_summary.json'
    if not cycle_summary_path.exists():
        return {}
    try:
        payload = json.loads(cycle_summary_path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    gate = dict(payload.get('gate', {}) or {})
    gate.setdefault('cycle_id', cycle_id)
    gate.setdefault('cycle_summary_path', str(cycle_summary_path))
    return gate


def _pick_best_run(hub_root: Path) -> Tuple[Dict[str, Any], Path]:
    registry_path = hub_root / 'registry' / 'experiment_registry.csv'
    frames: List[Tuple[str, pd.DataFrame]] = []
    cycle_df = _latest_cycle_results(hub_root=hub_root)
    if not cycle_df.empty:
        frames.append(('latest_cycle', cycle_df))
    if registry_path.exists():
        frames.append(('registry', pd.read_csv(registry_path)))
    if not frames:
        raise FileNotFoundError(f'未找到注册表: {registry_path}')

    skipped: List[str] = []
    for source_name, raw_df in frames:
        df = raw_df.copy()
        if df.empty:
            continue
        if 'status' in df.columns:
            df = df.loc[df['status'] == 'ok'].copy()
        if df.empty:
            continue
        df = _sort_score_frame(df)
        for _, series in df.iterrows():
            row = series.to_dict()
            run_dir = _resolve_run_dir(hub_root=hub_root, row=row)
            if run_dir is not None:
                row['selection_source'] = source_name
                return row, run_dir
            run_id = str(row.get('run_id', '') or '').strip()
            if run_id:
                skipped.append(run_id)
    if skipped:
        raise FileNotFoundError(f'未找到可用 run 目录，已跳过无效 run_id={skipped[:8]}')
    raise RuntimeError('当前没有可用于持仓建议的实验结果。')


def _read_positions(run_dir: Path) -> pd.DataFrame:
    path = run_dir / 'latest_portfolio_v1.csv'
    if not path.exists():
        raise FileNotFoundError(f'未找到最新组合文件: {path}')
    return pd.read_csv(path)


def _read_score_candidates(run_dir: Path) -> pd.DataFrame:
    path = run_dir / 'latest_scores.csv'
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _bootstrap_score_candidates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = df.copy()
    if 'ts_code' not in out.columns and 'code' in out.columns:
        out['ts_code'] = out['code'].map(_normalize_symbol)
    elif 'ts_code' in out.columns:
        out['ts_code'] = out['ts_code'].map(_normalize_symbol)
    if 'code' not in out.columns and 'ts_code' in out.columns:
        out['code'] = out['ts_code'].map(_ts_to_code)
    if 'portfolio_weight' not in out.columns:
        out['portfolio_weight'] = 0.02
    if 'target_exposure' not in out.columns:
        out['target_exposure'] = 0.3
    if 'cash_buffer' not in out.columns:
        out['cash_buffer'] = 0.7
    return out


def _diff_positions(prev_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    key_col = 'ts_code' if 'ts_code' in new_df.columns else ('code' if 'code' in new_df.columns else new_df.columns[0])
    prev = prev_df[[key_col, 'portfolio_weight']].copy() if (not prev_df.empty and 'portfolio_weight' in prev_df.columns) else pd.DataFrame(columns=[key_col, 'portfolio_weight'])
    prev = prev.rename(columns={'portfolio_weight': 'prev_weight'})
    now = new_df[[key_col, 'portfolio_weight']].copy()
    now = now.rename(columns={'portfolio_weight': 'target_weight'})
    merged = now.merge(prev, how='outer', on=key_col).fillna(0.0)
    merged['delta_weight'] = merged['target_weight'] - merged['prev_weight']
    merged['action'] = merged['delta_weight'].apply(lambda x: 'buy' if x > 1e-6 else ('sell' if x < -1e-6 else 'hold'))
    return merged.sort_values(['action', 'delta_weight'], ascending=[True, False])


def _symbol_col(df: pd.DataFrame) -> str:
    if 'ts_code' in df.columns:
        return 'ts_code'
    if 'code' in df.columns:
        return 'code'
    return str(df.columns[0])


def _assign_decision_weights(
    pos_df: pd.DataFrame,
    config: Dict[str, Any],
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    single_name_cap: float,
    total_exposure_cap: float,
    prev_df: pd.DataFrame | None = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """单遍决策定权（替代旧 trade_discipline/global_objective 连乘链）。

    把候选交给 engine.decision.decide_target_weights 一次定权：按分数分配、
    单名封顶取 min、regime 只过一次。约束以 config.decision_engine 为准，并与
    调用方传入的 cap 取 min（绝不放大）。

    Args:
        pos_df: 候选持仓表（含 portfolio_weight 列与一个分数列）。
        config: 运行时配置。
        market_state: 市场状态快照（取 regime）。
        account_ctx: 账户上下文（nav/cash）。
        single_name_cap: 调用方单名上限。
        total_exposure_cap: 调用方总仓位上限。
        prev_df: 上一次目标持仓（panic 时维持现有持仓的依据）。

    Returns:
        (pos_df, summary): 写好 portfolio_weight 的表 + 决策摘要。
    """
    from dataclasses import replace

    from live_execution_bridge.models import AccountState, Position

    if pos_df is None or pos_df.empty:
        return pos_df, {"applied": False, "reason": "empty_candidates"}

    sym_col = _symbol_col(pos_df)
    score_series = None
    for col in ("composite_score", "pred_score", "score", "seed_weight_norm", "portfolio_weight"):
        if col in pos_df.columns:
            score_series = pd.to_numeric(pos_df[col], errors="coerce").fillna(0.0)
            break
    if score_series is None:
        score_series = pd.Series(1.0, index=pos_df.index, dtype="float64")

    candidates = [
        {"symbol": str(pos_df.loc[idx, sym_col]), "score": float(score_series.loc[idx]), "raw": {}}
        for idx in pos_df.index
    ]

    cons = load_decision_constraints(config)
    cons = replace(
        cons,
        single_name_cap=min(cons.single_name_cap, float(single_name_cap)) if single_name_cap else cons.single_name_cap,
        total_exposure_cap=min(cons.total_exposure_cap, float(total_exposure_cap)) if total_exposure_cap else cons.total_exposure_cap,
    ).validated()

    # 现有持仓从上次目标表还原（panic 时 decide_target_weights 的目标=维持持仓）。
    # shares=1、last_price=权重×nav 只是把权重编码成市值，引擎只读 market_value()/nav()。
    nav_base = float(account_ctx.get("nav") or 0.0)
    if nav_base <= 0:
        nav_base = 1.0
    held_positions: list[Position] = []
    if prev_df is not None and not prev_df.empty and "portfolio_weight" in prev_df.columns:
        prev_sym_col = _symbol_col(prev_df)
        for _, row in prev_df.iterrows():
            try:
                w = float(row.get("portfolio_weight") or 0.0)
            except (TypeError, ValueError):
                continue
            sym = str(row.get(prev_sym_col, "") or "").strip()
            if w <= 0 or not sym:
                continue
            held_positions.append(Position(symbol=sym, shares=1, avg_cost=0.0, last_price=w * nav_base))
    acct = AccountState(
        account_id=str(account_ctx.get("account_id", "") or ""),
        cash=float(account_ctx.get("cash") or 0.0),
        nav_value=nav_base,
        positions=held_positions,
    )
    regime = (
        str(market_state.get("market_regime") or market_state.get("market_safety_regime") or "active")
    )

    result = decide_target_weights(candidates, acct, regime, cons)
    weight_map = {t.symbol: t.target_weight for t in result.targets}

    pos_df = pos_df.copy()
    # panic 目标里可能含不在今日候选中的持仓名，缺行会被下游 diff 判成清仓卖出——
    # 从 prev_df 把这些行带原列补回。
    missing_syms = sorted(set(weight_map) - set(pos_df[sym_col].astype(str)))
    if missing_syms and prev_df is not None and not prev_df.empty:
        prev_sym_col = _symbol_col(prev_df)
        carry = prev_df[prev_df[prev_sym_col].astype(str).isin(missing_syms)].copy()
        if not carry.empty:
            carry = carry.rename(columns={prev_sym_col: sym_col}) if prev_sym_col != sym_col else carry
            pos_df = pd.concat([pos_df, carry], ignore_index=True)
    pos_df["portfolio_weight"] = pos_df[sym_col].astype(str).map(weight_map).fillna(0.0)
    pos_df = pos_df[pd.to_numeric(pos_df["portfolio_weight"], errors="coerce").fillna(0.0) > 0].copy()
    return pos_df, {
        "applied": True,
        "posture": result.posture,
        "notes": list(result.notes),
        "single_name_cap": cons.single_name_cap,
        "total_exposure_cap": cons.total_exposure_cap,
        "max_names": cons.max_names,
        "n_selected": int(len(pos_df)),
    }


def _load_performance_feedback(config: Dict[str, Any], bridge_root: Path | None) -> Dict[str, Any]:
    if bridge_root is None:
        raw_root = str(config.get('paths', {}).get('bridge_root', '') or '').strip()
        bridge_root = Path(raw_root) if raw_root else None
    if bridge_root is None:
        return {}
    path = bridge_root / 'performance_feedback.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _filter_executable_candidates(df: pd.DataFrame, rec_cfg: Dict[str, Any], source_name: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    if out.empty or not bool(rec_cfg.get('enforce_executable_universe', True)):
        return out, {
            'source_name': str(source_name or ''),
            'enforced': bool(rec_cfg.get('enforce_executable_universe', True)),
            'kept_rows': int(len(out)),
            'dropped_rows': 0,
            'dropped_symbols': [],
        }
    allowed_suffixes = [
        str(item or '').strip().upper()
        for item in list(rec_cfg.get('executable_allowed_suffixes', ['.SH', '.SZ']) or ['.SH', '.SZ'])
        if str(item or '').strip()
    ]
    allowed_suffix_tuple = tuple(allowed_suffixes)
    key_col = _symbol_col(out)
    out['__symbol'] = out[key_col].map(_normalize_symbol)
    mask = out['__symbol'].astype(str).str.endswith(allowed_suffix_tuple)
    if bool(rec_cfg.get('require_tradable_basic', True)) and 'is_tradable_basic' in out.columns:
        mask &= pd.to_numeric(out['is_tradable_basic'], errors='coerce').fillna(0).gt(0)
    if 'is_st' in out.columns:
        mask &= pd.to_numeric(out['is_st'], errors='coerce').fillna(0).le(0)
    if 'is_suspended' in out.columns:
        mask &= pd.to_numeric(out['is_suspended'], errors='coerce').fillna(0).le(0)
    dropped = out.loc[~mask, '__symbol'].astype(str).tolist()
    kept = out.loc[mask].copy()
    kept = kept.drop(columns=['__symbol'], errors='ignore')
    return kept, {
        'source_name': str(source_name or ''),
        'enforced': True,
        'allowed_suffixes': allowed_suffixes,
        'require_tradable_basic': bool(rec_cfg.get('require_tradable_basic', True)),
        'kept_rows': int(len(kept.index)),
        'dropped_rows': int(len(dropped)),
        'dropped_symbols': dropped[:20],
    }


def _portfolio_limits(rec_cfg: Dict[str, Any], feedback: Dict[str, Any]) -> Dict[str, float]:
    override = dict(feedback.get('portfolio_overrides', {}) or {})
    return {
        'max_names': int(override.get('max_names', rec_cfg.get('max_names', 20)) or 20),
        'single_name_cap': float(override.get('single_name_cap', rec_cfg.get('single_name_cap', 0.10)) or 0.10),
        'total_exposure_cap': float(override.get('total_exposure_cap', rec_cfg.get('total_exposure_cap', 1.0)) or 1.0),
    }


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _load_account_sizing_context(config: Dict[str, Any]) -> Dict[str, Any]:
    trade_clock_root = Path(str(config.get('paths', {}).get('trade_clock_root', '') or '').strip())
    health = _load_json(trade_clock_root / 'latest_account_health.json') if str(trade_clock_root) else {}
    account_state = dict(health.get('account_state', {}) or {})
    broker = dict(health.get('broker', {}) or {})
    nav = float(account_state.get('nav') or account_state.get('total_asset') or 0.0)
    cash = float(account_state.get('cash') or account_state.get('available_cash') or 0.0)
    positions = list(account_state.get('positions') or [])
    return {
        'ok': bool(health.get('ok', False)),
        'account_id': str(account_state.get('account_id') or broker.get('account_id') or ''),
        'nav': nav,
        'cash': cash,
        'positions_count': int(health.get('positions_count', len(positions)) or 0),
    }


def _load_snapshot_prices(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=['ts_code', 'code', 'price', 'price_date', 'price_source'])
    df = pd.read_csv(path)
    if df.empty:
        return pd.DataFrame(columns=['ts_code', 'code', 'price', 'price_date', 'price_source'])
    if 'ts_code' not in df.columns and 'code' in df.columns:
        df = df.copy()
        df['ts_code'] = df['code']
    if 'code' not in df.columns and 'ts_code' in df.columns:
        df = df.copy()
        df['code'] = df['ts_code'].map(_ts_to_code)
    df['ts_code'] = df['ts_code'].astype(str).str.strip().str.upper()
    df['code'] = df['code'].map(_ts_to_code)
    if 'price' not in df.columns and 'close' in df.columns:
        df = df.copy()
        df['price'] = df['close']
    if 'date' in df.columns:
        df = df.copy()
        df['price_date'] = df['date']
    else:
        df['price_date'] = ''
    df['price_source'] = 'tushare_snapshot'
    return df[['ts_code', 'code', 'price', 'price_date', 'price_source']].copy()


def _iter_candidate_codes(df: pd.DataFrame) -> Iterable[str]:
    fields = [field for field in ['ts_code', 'code'] if field in df.columns]
    for field in fields:
        for value in df[field].dropna().astype(str).tolist():
            text = value.strip().upper()
            if not text:
                continue
            yield text if '.' in text else text.zfill(6)


def _ts_to_code(ts_code: str) -> str:
    text = str(ts_code or '').strip().upper()
    if not text:
        return ''
    return text.split('.', 1)[0] if '.' in text else text.zfill(6)


def _fallback_enriched_prices(enriched_dir: Path, symbols: Iterable[str]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        code = _ts_to_code(symbol)
        if not code:
            continue
        file_path = enriched_dir / f'{code}.csv'
        if not file_path.exists():
            continue
        try:
            df = pd.read_csv(file_path, usecols=['date', 'close'])
        except Exception:
            continue
        if df.empty:
            continue
        df = df.dropna(subset=['date', 'close']).sort_values('date')
        if df.empty:
            continue
        last = df.iloc[-1]
        rows.append(
            {
                'ts_code': symbol if '.' in str(symbol) else code,
                'code': code,
                'price': float(last['close']),
                'price_date': str(last['date']),
                'price_source': 'enriched_daily_fallback',
            }
        )
    return pd.DataFrame(rows)


def _attach_price_context(pos_df: pd.DataFrame, config: Dict[str, Any]) -> pd.DataFrame:
    market_cfg = dict(config.get('market_pipeline', {}) or {})
    snapshot_path = Path(str(market_cfg.get('price_snapshot_path', '') or '').strip())
    enriched_dir = Path(str(market_cfg.get('enriched_dir', '') or '').strip())
    price_df = _load_snapshot_prices(snapshot_path) if str(snapshot_path) else pd.DataFrame(columns=['ts_code', 'code', 'price', 'price_date', 'price_source'])

    symbols = list(dict.fromkeys(_iter_candidate_codes(pos_df)))
    if str(enriched_dir):
        missing = set(symbols)
        if not price_df.empty:
            missing = {item for item in symbols if item not in set(price_df['ts_code'].astype(str))}
        if missing:
            fallback_df = _fallback_enriched_prices(enriched_dir=enriched_dir, symbols=missing)
            if not fallback_df.empty:
                price_df = pd.concat([price_df, fallback_df], ignore_index=True)

    if price_df.empty:
        out = pos_df.copy()
        out['price'] = pd.NA
        out['price_date'] = ''
        out['price_source'] = ''
        return out

    price_df = price_df.drop_duplicates(subset=['ts_code', 'code'], keep='last').copy()
    out = pos_df.copy()
    key_col = _symbol_col(out)
    if key_col == 'code':
        out[key_col] = out[key_col].map(_ts_to_code)
    else:
        out[key_col] = out[key_col].astype(str).str.strip().str.upper()
    if key_col not in price_df.columns:
        if key_col == 'code' and 'ts_code' in out.columns:
            out['ts_code'] = out['ts_code'].astype(str).str.strip().str.upper()
            out = out.merge(price_df[['ts_code', 'price', 'price_date', 'price_source']], on='ts_code', how='left')
        else:
            out['price'] = pd.NA
            out['price_date'] = ''
            out['price_source'] = ''
            return out
    else:
        out = out.merge(price_df[[key_col, 'price', 'price_date', 'price_source']], on=key_col, how='left')
    if 'close' in out.columns:
        out['price'] = pd.to_numeric(out['price'], errors='coerce').fillna(pd.to_numeric(out['close'], errors='coerce'))
        out['price_source'] = out['price_source'].fillna('').mask(out['price_source'].fillna('').eq('') & out['close'].notna(), 'portfolio_close')
    return out


def _load_market_state_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(load_latest_market_state(config=config, allow_build=True) or {})


def _load_integrated_thesis_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(load_latest_integrated_thesis_state(config=config, allow_build=False) or {})


def _integrated_thesis_frame(config: Dict[str, Any]) -> pd.DataFrame:
    configured_root = str(config.get("integrated_thesis", {}).get("output_root", "") or "").strip()
    if configured_root:
        root = Path(configured_root).resolve()
    else:
        root = Path(str(config.get("paths", {}).get("research_root", "") or "")).resolve() / "integrated_thesis"
    path = root / "latest_integrated_thesis.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    if "symbol" in df.columns:
        df["ts_code"] = df["symbol"].astype(str).str.strip().str.upper()
    elif "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str).str.strip().str.upper()
    else:
        return pd.DataFrame()
    keep = [
        "ts_code",
        "integrated_thesis_score",
        "integrated_thesis_state",
        "primary_event_type",
        "primary_mechanism_group",
        "primary_event_fact_id",
        "primary_reason_chain",
        "mechanism_reason_chain",
        "earnings_reason",
        "earnings_reason_chain",
        "thesis_reason_chain",
        "thesis_gate_stage",
        "thesis_reject_reason",
    ]
    keep = [col for col in keep if col in df.columns]
    return df[keep].drop_duplicates(subset=["ts_code"], keep="first")


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    code = _ts_to_code(text)
    if not code:
        return ""
    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return f"{code}.SZ"
    if code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _load_router_signal_context(config: Dict[str, Any]) -> pd.DataFrame:
    root = Path(str(config.get("paths", {}).get("industry_router_output_root", "") or "")).resolve()
    path = root / "latest_stock_signal.csv"
    if not path.exists():
        return pd.DataFrame(columns=["ts_code", "mechanism_primary", "router_final_score", "router_allow_entry", "router_signal_state"])
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=["ts_code", "mechanism_primary", "router_final_score", "router_allow_entry", "router_signal_state"])
    if df.empty:
        return pd.DataFrame(columns=["ts_code", "mechanism_primary", "router_final_score", "router_allow_entry", "router_signal_state"])
    if "symbol" in df.columns and "ts_code" not in df.columns:
        df["ts_code"] = df["symbol"].astype(str).str.strip().str.upper()
    elif "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].astype(str).str.strip().str.upper()
    else:
        df["ts_code"] = ""
    df["router_final_score"] = pd.to_numeric(df["final_score"], errors="coerce").fillna(0.0) if "final_score" in df.columns else 0.0
    if "allow_entry" in df.columns:
        df["router_allow_entry"] = df["allow_entry"].fillna(True).astype(bool)
    else:
        df["router_allow_entry"] = True
    if "signal_state" in df.columns:
        df["router_signal_state"] = df["signal_state"].astype(str)
    else:
        df["router_signal_state"] = ""
    keep = [col for col in ["ts_code", "mechanism_primary", "router_final_score", "router_allow_entry", "router_signal_state"] if col in df.columns]
    return df[keep].drop_duplicates(subset=["ts_code"], keep="first")


def _tier_rank(value: Any) -> int:
    text = str(value or "").strip().upper()
    return {"A": 0, "B": 1, "C": 2, "F": 3}.get(text, 9)


def _normalize_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    values = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    vmax = float(values.max()) if not values.empty else 0.0
    vmin = float(values.min()) if not values.empty else 0.0
    spread = vmax - vmin
    if spread <= 1e-9:
        if vmax <= 1e-9:
            return pd.Series([0.0] * len(values.index), index=values.index, dtype="float64")
        return (values / vmax).clip(lower=0.0, upper=1.0)
    return ((values - vmin) / spread).clip(lower=0.0, upper=1.0)


def _candidate_tier_row(row: pd.Series) -> str:
    thesis_state = str(row.get("integrated_thesis_state") or "").strip().lower()
    thesis_score = float(row.get("integrated_thesis_score") or 0.0)
    router_score = float(row.get("router_final_score") or 0.0)
    fact_backed = bool(row.get("event_fact_backed", False))
    if fact_backed and thesis_state in {"build", "pilot"}:
        return "A"
    if fact_backed or thesis_state in {"build", "pilot", "watch"} or router_score >= 0.50 or thesis_score >= 0.48:
        return "B"
    if router_score >= 0.28 or thesis_score >= 0.30:
        return "C"
    return "F"


def _event_fact_backed_series(df: pd.DataFrame) -> pd.Series:
    if "primary_event_fact_id" not in df.columns:
        return pd.Series([False] * len(df.index), index=df.index, dtype="bool")
    return df["primary_event_fact_id"].astype(str).str.strip().ne("")


def _build_broad_candidate_pool(
    *,
    latest_portfolio_df: pd.DataFrame,
    latest_score_df: pd.DataFrame,
    integrated_thesis_df: pd.DataFrame,
    router_df: pd.DataFrame,
    max_rows: int,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    if not latest_portfolio_df.empty:
        frame = latest_portfolio_df.copy()
        if "ts_code" not in frame.columns and "code" in frame.columns:
            frame["ts_code"] = frame["code"].map(_normalize_symbol)
        elif "ts_code" in frame.columns:
            frame["ts_code"] = frame["ts_code"].astype(str).str.strip().str.upper()
        frame["source_latest_portfolio"] = 1.0
        frame["portfolio_seed_weight"] = pd.to_numeric(frame.get("portfolio_weight"), errors="coerce").fillna(0.0)
        frames.append(frame)
    if not latest_score_df.empty:
        frame = _bootstrap_score_candidates(latest_score_df.copy())
        frame["source_latest_scores"] = 1.0
        frame["score_seed"] = pd.to_numeric(frame.get("pred_score"), errors="coerce").fillna(0.0)
        frames.append(frame)
    if not integrated_thesis_df.empty:
        frame = integrated_thesis_df.copy()
        frame["source_integrated_thesis"] = 1.0
        frame["ts_code"] = frame["ts_code"].astype(str).str.strip().str.upper()
        if "portfolio_weight" not in frame.columns:
            frame["portfolio_weight"] = 0.0
        frames.append(frame)
    if not router_df.empty:
        frame = router_df.copy()
        frame["source_router"] = 1.0
        frame["portfolio_weight"] = 0.0
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined["ts_code"] = combined["ts_code"].astype(str).str.strip().str.upper()
    combined = combined.loc[combined["ts_code"] != ""].copy()
    combined["code"] = combined["ts_code"].map(_ts_to_code)
    for col in ["source_latest_portfolio", "source_latest_scores", "source_integrated_thesis", "source_router"]:
        if col not in combined.columns:
            combined[col] = 0.0
        combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0.0)
    combined["portfolio_seed_weight"] = pd.to_numeric(combined.get("portfolio_seed_weight"), errors="coerce").fillna(0.0)
    combined["score_seed"] = pd.to_numeric(combined.get("score_seed"), errors="coerce").fillna(0.0)
    if "portfolio_weight" in combined.columns:
        combined["portfolio_weight"] = pd.to_numeric(combined["portfolio_weight"], errors="coerce").fillna(0.0)
    grouped = combined.groupby("ts_code", as_index=False).agg(
        {
            "code": "last",
            "portfolio_weight": "max",
            "portfolio_seed_weight": "max",
            "score_seed": "max",
            "source_latest_portfolio": "max",
            "source_latest_scores": "max",
            "source_integrated_thesis": "max",
            "source_router": "max",
        }
    )
    return grouped.head(max(max_rows, 1)).copy()


def _market_adjusted_limits(limits: Dict[str, float], market_state: Dict[str, Any]) -> Dict[str, float]:
    risk_budget = float(market_state.get("risk_budget_multiplier", 1.0) or 1.0)
    adjusted = dict(limits)
    adjusted["base_max_names"] = int(limits["max_names"])
    adjusted["base_single_name_cap"] = float(limits["single_name_cap"])
    adjusted["base_total_exposure_cap"] = float(limits["total_exposure_cap"])
    adjusted["max_names"] = max(1, int(round(float(limits["max_names"]) * max(risk_budget, 0.35))))
    adjusted["single_name_cap"] = min(float(limits["single_name_cap"]), float(limits["total_exposure_cap"]) * max(risk_budget, 0.35))
    adjusted["total_exposure_cap"] = max(0.0, float(limits["total_exposure_cap"]) * max(risk_budget, 0.0))
    return adjusted


def _integrated_thesis_adjusted_limits(limits: Dict[str, float], integrated_state: Dict[str, Any]) -> Dict[str, float]:
    construction = dict(integrated_state.get("portfolio_construction", {}) or {})
    alpha_budget_multiplier = float(construction.get("alpha_budget_multiplier", 1.0) or 1.0)
    alpha_budget_multiplier = max(0.70, min(1.0, alpha_budget_multiplier))
    adjusted = dict(limits)
    adjusted["single_name_cap"] = round(float(adjusted["single_name_cap"]) * alpha_budget_multiplier, 6)
    adjusted["total_exposure_cap"] = round(float(adjusted["total_exposure_cap"]) * alpha_budget_multiplier, 6)
    return adjusted


def _attach_integrated_thesis_context(pos_df: pd.DataFrame, thesis_df: pd.DataFrame) -> pd.DataFrame:
    if pos_df.empty:
        return pos_df.copy()
    out = pos_df.copy()
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip().str.upper()
    elif "code" in out.columns:
        out["ts_code"] = out["code"].astype(str).map(_normalize_symbol)
    else:
        out["ts_code"] = ""
    if thesis_df.empty:
        out["integrated_thesis_score"] = 0.0
        out["integrated_thesis_state"] = ""
        out["primary_event_type"] = ""
        out["primary_mechanism_group"] = ""
        out["primary_event_fact_id"] = ""
        out["primary_reason_chain"] = ""
        out["mechanism_reason_chain"] = ""
        out["earnings_reason"] = ""
        out["earnings_reason_chain"] = ""
        out["thesis_reason_chain"] = ""
        out["thesis_gate_stage"] = ""
        out["thesis_reject_reason"] = ""
        return out
    out = out.merge(thesis_df, on="ts_code", how="left")
    for col, default in [
        ("integrated_thesis_score", 0.0),
        ("integrated_thesis_state", ""),
        ("primary_event_type", ""),
        ("primary_mechanism_group", ""),
        ("primary_event_fact_id", ""),
        ("primary_reason_chain", ""),
        ("mechanism_reason_chain", ""),
        ("earnings_reason", ""),
        ("earnings_reason_chain", ""),
        ("thesis_reason_chain", ""),
        ("thesis_gate_stage", ""),
        ("thesis_reject_reason", ""),
    ]:
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = out[col].fillna(default)
    return out


def _apply_candidate_controls(
    pos_df: pd.DataFrame,
    prev_df: pd.DataFrame,
    config: Dict[str, Any],
    market_state: Dict[str, Any],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    router_df = _load_router_signal_context(config=config)
    out = pos_df.copy()
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str).str.strip().str.upper()
    elif "code" in out.columns:
        out["ts_code"] = out["code"].astype(str).str.strip().map(_normalize_symbol)
    else:
        out["ts_code"] = ""
    if "code" in out.columns:
        out["code"] = out["code"].map(_ts_to_code)
    else:
        out["code"] = out["ts_code"].map(_ts_to_code)
    if not router_df.empty:
        out = out.merge(router_df, on="ts_code", how="left")
    tech_result = build_technical_confirmation_artifacts(
        config=config,
        candidate_df=out,
        prev_df=prev_df,
        market_state=market_state,
    )
    tech_df = pd.DataFrame(tech_result.get("frame", pd.DataFrame()))
    if not tech_df.empty:
        tech_df["ts_code"] = tech_df["ts_code"].astype(str).str.strip().str.upper()
        tech_cols = [col for col in tech_df.columns if col != "code"]
        out = out.merge(tech_df[tech_cols], on="ts_code", how="left")
    for col, default in [
        ("router_final_score", 0.0),
        ("router_allow_entry", True),
        ("router_signal_state", ""),
        ("mechanism_primary", ""),
        ("tech_trend_score", 0.0),
        ("tech_volume_score", 0.0),
        ("tech_stretch_penalty", 0.0),
        ("tech_hold_health", 0.0),
        ("tech_final_score", 0.0),
        ("tech_allow_entry", True),
        ("tech_gate_reason", "tech_unavailable"),
        ("tech_entry_style", "pullback"),
        ("tech_weight_multiplier", 1.0),
        ("is_existing_position", False),
    ]:
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = out[col].fillna(default)

    mechanism_multipliers = dict(market_state.get("mechanism_multipliers", {}) or {})
    new_position_policy = str(market_state.get("new_position_policy", "allow") or "allow")
    out["base_portfolio_weight"] = pd.to_numeric(out.get("portfolio_weight"), errors="coerce").fillna(0.0)
    out["mechanism_bias_multiplier"] = out["mechanism_primary"].map(lambda x: float(mechanism_multipliers.get(str(x or ""), 1.0) or 1.0))
    out["market_weight_multiplier"] = 1.0
    if new_position_policy == "tight":
        out.loc[(~out["is_existing_position"].astype(bool)) & (out["tech_entry_style"].astype(str) == "wait"), "market_weight_multiplier"] = 0.55
        out.loc[(~out["is_existing_position"].astype(bool)) & (out["tech_entry_style"].astype(str) == "pilot"), "market_weight_multiplier"] = 0.70
    elif new_position_policy in {"no_new_positions", "reduce_only"}:
        out.loc[(~out["is_existing_position"].astype(bool)), "market_weight_multiplier"] = 0.0

    out.loc[(~out["router_allow_entry"].astype(bool)) & (~out["is_existing_position"].astype(bool)), "market_weight_multiplier"] *= 0.38
    out.loc[(~out["tech_allow_entry"].astype(bool)) & (~out["is_existing_position"].astype(bool)), "market_weight_multiplier"] *= 0.42
    out["portfolio_weight"] = (
        out["base_portfolio_weight"]
        * pd.to_numeric(out["tech_weight_multiplier"], errors="coerce").fillna(1.0)
        * pd.to_numeric(out["mechanism_bias_multiplier"], errors="coerce").fillna(1.0)
        * pd.to_numeric(out["market_weight_multiplier"], errors="coerce").fillna(1.0)
    )
    out["portfolio_weight"] = pd.to_numeric(out["portfolio_weight"], errors="coerce").fillna(0.0)
    out = out.sort_values(["portfolio_weight", "base_portfolio_weight", "tech_final_score"], ascending=[False, False, False]).reset_index(drop=True)
    control_summary = {
        "market_state": {
            "market_regime": str(market_state.get("market_regime", "") or ""),
            "style_bias": str(market_state.get("style_bias", "") or ""),
            "mechanism_bias": str(market_state.get("mechanism_bias", "") or ""),
            "risk_budget_multiplier": float(market_state.get("risk_budget_multiplier", 1.0) or 1.0),
            "turnover_multiplier": float(market_state.get("turnover_multiplier", 1.0) or 1.0),
            "entry_strictness": float(market_state.get("entry_strictness", 0.5) or 0.5),
            "new_position_policy": new_position_policy,
        },
        "technical_confirmation": dict(tech_result.get("summary", {}) or {}),
        "artifacts": {
            "technical_confirmation_path": str(tech_result.get("latest_path", "") or ""),
            "technical_confirmation_summary_path": str(tech_result.get("summary_path", "") or ""),
        },
    }
    return out, control_summary


def build_portfolio_recommendation(config: Dict[str, Any], bridge_root: Path | None = None) -> Dict[str, Any]:
    runtime_cfg = dict(config.get('research_brain', {}) or {})
    rec_cfg = dict(config.get('portfolio_recommendation', {}) or {})
    candidate_llm_cfg = dict(config.get('portfolio_candidate_llm_review', {}) or {})
    feedback = _load_performance_feedback(config=config, bridge_root=bridge_root)
    limits = _portfolio_limits(rec_cfg=rec_cfg, feedback=feedback)
    account_ctx = _load_account_sizing_context(config=config)
    hub_root = Path(str(runtime_cfg.get('hub_output_root', '') or '').strip())
    out_root = ensure_dir(Path(str(config['paths'].get('portfolio_output_root', '') or '').strip()))
    cycle_gate = _latest_cycle_gate(hub_root=hub_root)
    research_deployment_ready = bool(cycle_gate.get('deployment_ready', True)) if cycle_gate else True
    research_deployment_reason = str(cycle_gate.get('reason', '') or '')
    row, run_dir = _pick_best_run(hub_root=hub_root)
    prev_path = out_root / 'target_positions_prev.csv'
    prev_df = pd.read_csv(prev_path) if prev_path.exists() else pd.DataFrame()
    prev_reset_reason = ''
    if int(account_ctx.get('positions_count') or 0) <= 0 and not prev_df.empty:
        prev_df = pd.DataFrame()
        prev_reset_reason = 'empty_account_reset_prev_targets'
    market_state = _load_market_state_summary(config=config)
    integrated_thesis_state = _load_integrated_thesis_summary(config=config)
    integrated_thesis_df = _integrated_thesis_frame(config=config)
    adjusted_limits = _market_adjusted_limits(limits=limits, market_state=market_state) if bool(rec_cfg.get("market_state_aware_sizing", True)) else dict(limits)
    if bool(config.get("integrated_thesis", {}).get("portfolio_budget_overlay", True)):
        adjusted_limits = _integrated_thesis_adjusted_limits(limits=adjusted_limits, integrated_state=integrated_thesis_state)
    max_names = int(adjusted_limits['max_names'])
    single_name_cap = float(adjusted_limits['single_name_cap'])
    total_exposure_cap = float(adjusted_limits['total_exposure_cap'])
    broad_candidate_limit = int(rec_cfg.get('broad_candidate_pool_limit', 48) or 48)
    raw_portfolio_df = _read_positions(run_dir=run_dir).head(max(max_names * 4, broad_candidate_limit)).copy()
    raw_score_df = _read_score_candidates(run_dir=run_dir)
    raw_score_df = raw_score_df.sort_values('pred_score', ascending=False) if ('pred_score' in raw_score_df.columns and not raw_score_df.empty) else raw_score_df
    raw_score_df = raw_score_df.head(max(broad_candidate_limit * 2, max_names * 6)).copy()
    pos_df, execution_filter = _filter_executable_candidates(raw_portfolio_df, rec_cfg=rec_cfg, source_name='latest_portfolio_v1')
    candidate_source = 'latest_portfolio_v1'
    if pos_df.empty:
        score_df = _bootstrap_score_candidates(raw_score_df.copy())
        score_df = score_df.sort_values('pred_score', ascending=False) if 'pred_score' in score_df.columns else score_df
        score_df = score_df.head(max_names * 4).copy()
        score_df, score_execution_filter = _filter_executable_candidates(score_df, rec_cfg=rec_cfg, source_name='latest_scores')
        if not score_df.empty:
            pos_df = score_df
            candidate_source = 'latest_scores_executable_fallback'
            execution_filter = score_execution_filter
    fallback_candidate_df = pos_df.copy()
    router_df = _load_router_signal_context(config=config)
    broad_pool_seed = _build_broad_candidate_pool(
        latest_portfolio_df=raw_portfolio_df,
        latest_score_df=raw_score_df,
        integrated_thesis_df=integrated_thesis_df,
        router_df=router_df,
        max_rows=max(broad_candidate_limit * 2, max_names * 8),
    )
    broad_pool_seed, broad_execution_filter = _filter_executable_candidates(
        _bootstrap_score_candidates(broad_pool_seed),
        rec_cfg=rec_cfg,
        source_name='broad_candidate_pool',
    )
    broad_pool_df = _attach_price_context(pos_df=broad_pool_seed, config=config)
    broad_pool_df = _attach_integrated_thesis_context(pos_df=broad_pool_df, thesis_df=integrated_thesis_df)
    if not broad_pool_df.empty:
        if "router_final_score" not in broad_pool_df.columns:
            broad_pool_df = broad_pool_df.merge(router_df, on="ts_code", how="left")
        broad_pool_df["event_fact_backed"] = _event_fact_backed_series(broad_pool_df)
        broad_pool_df["seed_weight_norm"] = _normalize_series(broad_pool_df, "portfolio_seed_weight")
        broad_pool_df["pred_score_norm"] = _normalize_series(broad_pool_df, "score_seed")
        broad_pool_df["router_score_norm"] = _normalize_series(broad_pool_df, "router_final_score")
        broad_pool_df["thesis_score_norm"] = _normalize_series(broad_pool_df, "integrated_thesis_score")
        if bool(rec_cfg.get("hard_data_candidate_pool_enabled", True)):
            hard_weights = dict(rec_cfg.get("hard_data_candidate_weights", {}) or {})
            seed_w = float(hard_weights.get("seed_weight", 0.18) or 0.0)
            pred_w = float(hard_weights.get("pred_score", 0.42) or 0.0)
            valuation_w = float(hard_weights.get("valuation", 0.24) or 0.0)
            liquidity_w = float(hard_weights.get("liquidity", 0.16) or 0.0)
            total_w = max(seed_w + pred_w + valuation_w + liquidity_w, 1e-9)
            broad_pool_df["selection_score"] = (
                broad_pool_df["seed_weight_norm"] * seed_w
                + broad_pool_df["pred_score_norm"] * pred_w
                + _series_or_zero(broad_pool_df, "valuation_signal_score") * valuation_w
                + _series_or_zero(broad_pool_df, "liquidity_signal_score") * liquidity_w
            ) / total_w
            broad_pool_df["candidate_pool_basis"] = "hard_data_only"
        else:
            broad_pool_df["selection_score"] = (
                broad_pool_df["seed_weight_norm"] * 0.24
                + broad_pool_df["pred_score_norm"] * 0.28
                + broad_pool_df["router_score_norm"] * 0.18
                + broad_pool_df["thesis_score_norm"] * 0.22
                + broad_pool_df["event_fact_backed"].astype(float) * 0.08
            )
            broad_pool_df["candidate_pool_basis"] = "mixed_thesis_router_event"
        broad_pool_df["candidate_tier"] = broad_pool_df.apply(_candidate_tier_row, axis=1)
    pos_df = _attach_price_context(pos_df=pos_df, config=config)
    pos_df = _attach_integrated_thesis_context(pos_df=pos_df, thesis_df=integrated_thesis_df)
    if not pos_df.empty:
        pos_df["event_fact_backed"] = _event_fact_backed_series(pos_df)
        if "router_final_score" not in pos_df.columns:
            pos_df = pos_df.merge(router_df, on="ts_code", how="left")
        pos_df["candidate_tier"] = pos_df.apply(_candidate_tier_row, axis=1)
    thesis_summary = dict((integrated_thesis_state.get('summary') or {}) or {})
    broad_pool_df, activation_summary, llm_candidate_review, outer_intelligence_summary = activate_and_rank_candidate_pool(
        broad_pool_df=broad_pool_df,
        config=config,
        rec_cfg=rec_cfg,
        candidate_llm_cfg=candidate_llm_cfg,
        market_state=market_state,
        account_ctx=account_ctx,
        thesis_summary=thesis_summary,
    )
    if bool(rec_cfg.get("hard_data_candidate_pool_enabled", True)) and not broad_pool_df.empty:
        hard_weights = dict(rec_cfg.get("hard_data_candidate_weights", {}) or {})
        seed_w = float(hard_weights.get("seed_weight", 0.18) or 0.0)
        pred_w = float(hard_weights.get("pred_score", 0.42) or 0.0)
        valuation_w = float(hard_weights.get("valuation", 0.24) or 0.0)
        liquidity_w = float(hard_weights.get("liquidity", 0.16) or 0.0)
        total_w = max(seed_w + pred_w + valuation_w + liquidity_w, 1e-9)
        broad_pool_df["selection_score"] = (
            _series_or_zero(broad_pool_df, "seed_weight_norm") * seed_w
            + _series_or_zero(broad_pool_df, "pred_score_norm") * pred_w
            + _series_or_zero(broad_pool_df, "valuation_signal_score") * valuation_w
            + _series_or_zero(broad_pool_df, "liquidity_signal_score") * liquidity_w
        ) / total_w
        broad_pool_df["candidate_pool_basis"] = "hard_data_only"
    broad_pool_df = apply_candidate_llm_overlay(broad_pool_df, llm_candidate_review, candidate_llm_cfg)
    broad_pool_df = sort_candidate_pool(
        broad_pool_df,
        include_outer_priority=True,
        include_activation_priority=True,
        include_pred_score=True,
    )
    pos_df, candidate_source, execution_filter = choose_candidate_pool(
        current_df=pos_df,
        candidate_source=candidate_source,
        broad_pool_df=broad_pool_df,
        broad_candidate_limit=broad_candidate_limit,
        max_names=max_names,
        rec_cfg=rec_cfg,
        thesis_summary=thesis_summary,
        llm_review=llm_candidate_review,
        broad_execution_filter=broad_execution_filter,
    )
    control_summary: Dict[str, Any] = {}
    if bool(rec_cfg.get("technical_confirmation_gate", True)) and not bool(rec_cfg.get("intelligent_outer_allocator_replaces_internal_gates", True)):
        pos_df, control_summary = _apply_candidate_controls(
            pos_df=pos_df,
            prev_df=prev_df,
            config=config,
            market_state=market_state,
        )
    pre_v2a_candidate_df = pos_df.copy()
    broker_cfg = dict(config.get('broker', {}) or {})
    if not broker_cfg:
        broker_cfg = dict(dict(config.get('execution_bridge_runtime', {}) or {}).get('broker', {}) or {})
    adjusted_limits, account_sizing = account_size_adjusted_limits(
        limits=adjusted_limits,
        rec_cfg=rec_cfg,
        broker_cfg=broker_cfg,
        account_ctx=account_ctx,
        candidate_df=pos_df,
    )
    max_names = int(adjusted_limits['max_names'])
    single_name_cap = float(adjusted_limits['single_name_cap'])
    total_exposure_cap = float(adjusted_limits['total_exposure_cap'])
    account_profile = account_allocator_profile(account_ctx=account_ctx, limits=adjusted_limits)
    v2a_result = build_portfolio_artifacts(
        config=config,
        candidate_df=pos_df,
        prev_df=prev_df,
        market_state=market_state,
        portfolio_limits=adjusted_limits,
    )
    if bool(v2a_result.get("ok", False)) and not pd.DataFrame(v2a_result.get("frame", pd.DataFrame())).empty:
        pos_df = pd.DataFrame(v2a_result.get("frame", pd.DataFrame())).copy()
        control_summary["portfolio"] = dict(v2a_result.get("summary", {}) or {})
        control_summary["portfolio_posture"] = dict(v2a_result.get("posture", {}) or {})
        control_summary.setdefault("artifacts", {}).update(dict(v2a_result.get("artifacts", {}) or {}))
    account_size_v2a_fallback = ''
    selection_input_df = pos_df
    if (
        bool(account_sizing.get('applied'))
        and int(account_ctx.get('positions_count') or 0) <= 0
        and int(account_sizing.get('adjusted_max_names') or 0) > 1
    ):
        selection_input_df = pre_v2a_candidate_df.copy()
        account_size_v2a_fallback = 'small_account_prefers_pre_v2a_candidates'
    pos_df = pos_df.loc[pd.to_numeric(pos_df.get('portfolio_weight'), errors='coerce').fillna(0.0) > 0].copy()
    fallback_retained = False
    if pos_df.empty and not fallback_candidate_df.empty:
        pos_df = _attach_price_context(pos_df=fallback_candidate_df.head(1).copy(), config=config)
        pos_df['portfolio_weight'] = min(max(total_exposure_cap, 0.02), single_name_cap, 0.02)
        pos_df['tech_gate_reason'] = 'all_candidates_filtered_fallback_retained'
        pos_df['tech_entry_style'] = 'wait'
        pos_df['tech_allow_entry'] = False
        pos_df['tech_weight_multiplier'] = 0.5
        fallback_retained = True
    pos_df, account_candidate_selection = select_account_aware_candidates(
        df=selection_input_df,
        max_names=max_names,
        total_exposure_cap=total_exposure_cap,
        nav=float(account_ctx.get('nav') or 0.0),
        lot_size=max(int(broker_cfg.get('lot_size', 100) or 100), 1),
        min_trade_value=max(float(broker_cfg.get('min_trade_value', 2000.0) or 2000.0), 0.0),
        weight_buffer=min(max(float(rec_cfg.get('account_size_min_weight_buffer', 1.05) or 1.05), 1.0), 2.0),
    )
    preliminary_alpha_attribution = build_alpha_attribution(pos_df, prev_df)
    preliminary_alpha_lifecycle = build_alpha_lifecycle(
        candidate_df=broad_pool_df,
        target_df=pos_df,
        position_df=prev_df,
        alpha_registry=dict((activation_summary.get('alpha_registry') or {}) or {}),
        alpha_attribution=preliminary_alpha_attribution,
        market_state=market_state,
    )
    operating_brain = build_operating_brain(
        config=config,
        market_state=market_state,
        account_ctx=account_ctx,
        candidate_df=broad_pool_df,
        alpha_lifecycle=preliminary_alpha_lifecycle,
        summary={},
    )
    # 旧的 trade_discipline 连乘链已移除，改为单遍决策引擎定权。
    preliminary_trade_discipline = {"applied": False, "reason": "legacy_discipline_layer_removed"}
    pos_df = pos_df.head(max_names).copy()
    pos_df = clean_target_position_columns(pos_df)
    # 这些旧分配器一律置为未启用占位（保留 summary 字段形状，下游/审计不报错）。
    registered_family_allocator = {'applied': False, 'reason': 'decision_engine_single_pass'}
    alpha_lifecycle_allocator = {'applied': False, 'reason': 'decision_engine_single_pass'}
    trade_discipline_allocator = {'applied': False, 'reason': 'decision_engine_single_pass'}
    evidence_gate_summary = {'applied': False, 'reason': 'decision_engine_single_pass'}
    diversification_summary = {'applied': False, 'reason': 'decision_engine_single_pass'}
    pre_release_proxy_summary: Dict[str, Any] = {'applied': False, 'reason': 'decision_engine_single_pass'}
    reweight_before = 0.0
    reweight_after = 0.0
    if 'portfolio_weight' in pos_df.columns:
        # 单遍定权：按分数分配、单名封顶取 min、regime 只过一次。
        pos_df, decision_weight_summary = _assign_decision_weights(
            pos_df=pos_df,
            config=config,
            market_state=market_state,
            account_ctx=account_ctx,
            single_name_cap=single_name_cap,
            total_exposure_cap=total_exposure_cap,
            prev_df=prev_df,
        )
        reweight_before = float(pos_df['portfolio_weight'].sum()) if not pos_df.empty else 0.0
        # 证据闸门/盘中代理目标不是被删的仲裁塔，是独立的惩罚层；开关在各自函数/配置里。
        pos_df, evidence_gate_summary = apply_evidence_gate(pos_df, config=config)
        if bool(rec_cfg.get('pre_release_intraday_proxy_objective_enabled', False)):
            try:
                pos_df, pre_release_proxy_summary = apply_pre_release_proxy_objective(
                    pos_df=pos_df,
                    prev_df=prev_df,
                    config=config,
                    rec_cfg=rec_cfg,
                    account_ctx=account_ctx,
                    total_exposure_cap=float(total_exposure_cap),
                    single_name_cap=float(single_name_cap),
                )
            except Exception as exc:
                pre_release_proxy_summary = {
                    'applied': False,
                    'error': str(exc),
                    'fail_open': bool(rec_cfg.get('pre_release_proxy_fail_open', True)),
                }
                if not bool(rec_cfg.get('pre_release_proxy_fail_open', True)):
                    raise
        # 仅保留机械的可执行下限/取整（纯落地约束，不是砍仓层）。
        pos_df, executable_floor_summary = enforce_min_executable_weights(
            df=pos_df,
            total_exposure_cap=total_exposure_cap,
            single_name_cap=single_name_cap,
            nav=float(account_ctx.get('nav') or 0.0),
            lot_size=max(int(broker_cfg.get('lot_size', 100) or 100), 1),
            min_trade_value=max(float(broker_cfg.get('min_trade_value', 2000.0) or 2000.0), 0.0),
            weight_buffer=min(max(float(rec_cfg.get('account_size_min_weight_buffer', 1.05) or 1.05), 1.0), 2.0),
        )
        reweight_after = float(pos_df['portfolio_weight'].sum()) if not pos_df.empty else 0.0
        total_weight = reweight_after
    else:
        decision_weight_summary = {'applied': False, 'reason': 'missing_portfolio_weight'}
        executable_floor_summary = {'applied': False, 'reason': 'missing_portfolio_weight'}
        total_weight = 0.0
    rebalance_df = _diff_positions(prev_df=prev_df, new_df=pos_df)
    if not rebalance_df.empty:
        key_col = _symbol_col(pos_df)
        extra_cols = [col for col in [key_col, 'price', 'price_date', 'price_source'] if col in pos_df.columns]
        rebalance_df = rebalance_df.merge(pos_df[extra_cols], on=key_col, how='left')

    bridge_context = {}
    if bridge_root is not None:
        ctx_path = bridge_root / 'enriched_context.json'
        if ctx_path.exists():
            try:
                bridge_context = json.loads(ctx_path.read_text(encoding='utf-8'))
            except Exception:
                bridge_context = {}

    price_covered = int(pd.to_numeric(pos_df.get('price'), errors='coerce').fillna(0).gt(0).sum()) if 'price' in pos_df.columns else 0
    missing_price_symbols = []
    if 'price' in pos_df.columns:
        key_col = _symbol_col(pos_df)
        missing_price_symbols = pos_df.loc[pd.to_numeric(pos_df['price'], errors='coerce').fillna(0) <= 0, key_col].astype(str).tolist()
    alpha_attribution = build_alpha_attribution(pos_df, prev_df)
    alpha_lifecycle = build_alpha_lifecycle(
        candidate_df=broad_pool_df,
        target_df=pos_df,
        position_df=prev_df,
        alpha_registry=dict((activation_summary.get('alpha_registry') or {}) or {}),
        alpha_attribution=alpha_attribution,
        market_state=market_state,
    )
    trade_discipline = dict(preliminary_trade_discipline)

    summary = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy_name': str(row.get('strategy_name', '')),
        'strategy_key': str(row.get('strategy_key', '')),
        'run_id': str(row.get('run_id', '')),
        'run_dir': str(run_dir),
        'selection_source': str(row.get('selection_source', 'registry')),
        'total_score': float(row.get('total_score', 0.0) or 0.0),
        'sharpe': float(row.get('sharpe', 0.0) or 0.0),
        'max_drawdown': float(row.get('max_drawdown', 0.0) or 0.0),
        'gpu_used': bool(row.get('gpu_used', False)),
        'n_names': int(len(pos_df)),
        'price_coverage': {
            'covered': price_covered,
            'total': int(len(pos_df)),
            'coverage_ratio': float(price_covered / max(len(pos_df), 1)),
            'missing_symbols': missing_price_symbols,
        },
        'simulation_ready': bool(
            research_deployment_ready
            and (
                not rec_cfg.get('simulation_ready_need_gate', False)
                or float(row.get('total_score', 0.0) or 0.0) >= 45.0
            )
        ),
        'research_deployment_ready': bool(research_deployment_ready),
        'research_deployment_gate': cycle_gate,
        'research_deployment_reason': research_deployment_reason,
        'portfolio_limits': adjusted_limits,
        'account_sizing': account_sizing,
        'account_allocator_profile': account_profile,
        'previous_target_reset_reason': prev_reset_reason,
        'candidate_source': candidate_source,
        'account_size_v2a_fallback': account_size_v2a_fallback,
        'execution_candidate_filter': execution_filter,
        'candidate_pool_stats': summarize_candidate_pool(
            broad_pool_df=broad_pool_df,
            broad_candidate_limit=broad_candidate_limit,
            llm_review=llm_candidate_review,
        ),
        'strategy_activation': activation_summary,
        'candidate_pool_llm_review': llm_candidate_review,
        'outer_intelligence_allocator': outer_intelligence_summary,
        'alpha_attribution': alpha_attribution,
        'alpha_lifecycle': alpha_lifecycle,
        'registered_family_allocator': registered_family_allocator,
        'alpha_lifecycle_allocator': alpha_lifecycle_allocator,
        'trade_discipline': trade_discipline,
        'trade_discipline_allocator': trade_discipline_allocator,
        'evidence_audit_gate': evidence_gate_summary,
        'portfolio_weight_totals': {
            'final_total_weight': float(total_weight),
            'reweight_before': float(reweight_before),
            'reweight_after': float(reweight_after),
            'target_fill': float(target_fill) if 'target_fill' in locals() else 0.0,
        },
        'diversification_objective': diversification_summary,
        'decision_weight_summary': decision_weight_summary,
        'executable_weight_floor': executable_floor_summary,
        'pre_release_proxy_objective': pre_release_proxy_summary,
        'account_candidate_selection': account_candidate_selection,
        'portfolio': dict(control_summary.get('portfolio', {}) or {}),
        'portfolio_posture': dict(control_summary.get('portfolio_posture', {}) or {}),
        'market_state': dict(control_summary.get('market_state', {}) or market_state),
        'formal_strategy_framework': str(integrated_thesis_state.get('formal_strategy_framework', 'integrated_event_industry_earnings_alpha') or 'integrated_event_industry_earnings_alpha'),
        'primary_strategy_key': str(integrated_thesis_state.get('primary_strategy_key', '') or ''),
        'integrated_thesis_state': dict(integrated_thesis_state),
        'technical_confirmation': dict(control_summary.get('technical_confirmation', {}) or {}),
        'artifacts': {
            **dict(control_summary.get('artifacts', {}) or {}),
            'market_state_path': str(Path(str(config['paths'].get('market_state_root', '') or '')) / 'latest_market_state.json'),
            'integrated_thesis_state_path': str(Path(str(config['paths'].get('research_root', '') or '')) / 'integrated_thesis' / 'integrated_thesis_state.json'),
            'candidate_pool_path': str(out_root / 'candidate_pool.csv'),
            'evidence_audit_summary_path': str(Path(str(dict(config.get('evidence_audit', {}) or {}).get('artifact_root') or (out_root / 'evidence_audit_v1'))) / 'latest' / 'evidence_audit_summary.json'),
            'evidence_audit_reviews_path': str(Path(str(dict(config.get('evidence_audit', {}) or {}).get('artifact_root') or (out_root / 'evidence_audit_v1'))) / 'latest' / 'evidence_audit_reviews.json'),
            'evidence_audit_sources_path': str(Path(str(dict(config.get('evidence_audit', {}) or {}).get('artifact_root') or (out_root / 'evidence_audit_v1'))) / 'latest' / 'evidence_audit_sources.json'),
        },
        'fallback_retained_due_all_filtered': bool(fallback_retained),
        'performance_feedback': feedback,
        'research_context': bridge_context,
    }
    summary['llm_operating_brain'] = operating_brain

    target_path = out_root / 'target_positions.csv'
    rebalance_path = out_root / 'rebalance_orders.csv'
    candidate_pool_path = out_root / 'candidate_pool.csv'
    summary_path = out_root / 'portfolio_recommendation.json'
    harvest_risk_path = out_root / 'harvest_risk_assessment.json'
    econometric_guardrails_path = out_root / 'econometric_guardrails.json'
    objective_snapshot_path = out_root / 'global_objective_snapshot.json'
    # 旧三联打分引擎（global_objective/harvest_risk/econometric_guardrails）已移除，
    # 集中度/风险约束已在 decision engine 单遍内完成。保留空占位与文件路径以兼容下游。
    harvest_risk: Dict[str, Any] = {"applied": False, "reason": "merged_into_decision_engine"}
    econometric_guardrails: Dict[str, Any] = {"applied": False, "reason": "merged_into_decision_engine"}
    global_objective: Dict[str, Any] = {"applied": False, "reason": "merged_into_decision_engine"}
    summary['harvest_risk'] = harvest_risk
    summary['econometric_guardrails'] = econometric_guardrails
    summary['global_objective'] = global_objective
    summary['artifacts']['harvest_risk_assessment_path'] = str(harvest_risk_path)
    summary['artifacts']['econometric_guardrails_path'] = str(econometric_guardrails_path)
    summary['artifacts']['global_objective_snapshot_path'] = str(objective_snapshot_path)
    pos_df.to_csv(target_path, index=False, encoding='utf-8-sig')
    rebalance_df.to_csv(rebalance_path, index=False, encoding='utf-8-sig')
    if not broad_pool_df.empty:
        sort_candidate_pool(
            broad_pool_df,
            include_outer_priority=True,
            include_activation_priority=True,
            include_pred_score=False,
        ).to_csv(candidate_pool_path, index=False, encoding='utf-8-sig')
    else:
        pd.DataFrame().to_csv(candidate_pool_path, index=False, encoding='utf-8-sig')
    harvest_risk_path.write_text(json.dumps(harvest_risk, ensure_ascii=False, indent=2), encoding='utf-8')
    econometric_guardrails_path.write_text(json.dumps(econometric_guardrails, ensure_ascii=False, indent=2), encoding='utf-8')
    objective_snapshot_path.write_text(json.dumps(global_objective, ensure_ascii=False, indent=2), encoding='utf-8')
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    pos_df.to_csv(prev_path, index=False, encoding='utf-8-sig')
    return {
        'summary_path': str(summary_path),
        'target_positions_path': str(target_path),
        'rebalance_orders_path': str(rebalance_path),
        'n_names': int(len(pos_df)),
        'run_id': str(row.get('run_id', '')),
        'market_regime': str(summary.get('market_state', {}).get('market_regime', '') or ''),
        'style_bias': str(summary.get('market_state', {}).get('style_bias', '') or ''),
        'tech_allow_count': int(summary.get('technical_confirmation', {}).get('allow_count', 0) or 0),
        'fallback_retained': bool(summary.get('fallback_retained_due_all_filtered', False)),
        'simulation_ready': bool(summary.get('simulation_ready', False)),
        'research_deployment_ready': bool(summary.get('research_deployment_ready', False)),
        'research_deployment_reason': str(summary.get('research_deployment_reason', '') or ''),
    }
