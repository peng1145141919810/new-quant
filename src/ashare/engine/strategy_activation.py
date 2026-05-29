from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import pandas as pd

from .alpha_registry import enrich_alpha_registry, summarize_alpha_registry
from .config_utils import ensure_dir
from .llm_trace import write_llm_trace
from .llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_symbol(value: Any) -> str:
    text = _text(value).upper()
    if not text:
        return ""
    if "." in text:
        code, suffix = text.split(".", 1)
        return f"{code.zfill(6)}.{suffix}"
    return text.zfill(6)


def _ts_to_code(value: Any) -> str:
    symbol = _normalize_symbol(value)
    if not symbol:
        return ""
    return symbol.split(".", 1)[0]


def _normalize_series(df: pd.DataFrame, column: str, *, invert: bool = False) -> pd.Series:
    if column not in df.columns or df.empty:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    series = pd.to_numeric(df[column], errors="coerce")
    valid = series.dropna()
    if valid.empty:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    lo = float(valid.min())
    hi = float(valid.max())
    if hi - lo <= 1e-9:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    else:
        out = (series - lo) / (hi - lo)
        out = out.fillna(0.0).clip(lower=0.0, upper=1.0)
    return (1.0 - out).clip(lower=0.0, upper=1.0) if invert else out.clip(lower=0.0, upper=1.0)


def _numeric_col(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce").fillna(0.0)


def _weight_map(cfg: Dict[str, Any], key: str, defaults: Dict[str, float]) -> Dict[str, float]:
    raw = dict(cfg.get(key, {}) or {})
    out: Dict[str, float] = {}
    for item_key, default_value in defaults.items():
        out[item_key] = max(0.0, _safe_float(raw.get(item_key), default_value))
    total = sum(out.values())
    if total <= 1e-9:
        return dict(defaults)
    return {item_key: value / total for item_key, value in out.items()}


def _bounded_value(cfg: Dict[str, Any], key: str, default: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, _safe_float(cfg.get(key), default)))


def _signal_weight_sets(cfg: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    defaults = {
        "valuation": {
            "pe_pct_industry": 0.35,
            "pb_pct_industry": 0.35,
            "ps_pct_industry": 0.30,
        },
        "revision": {
            "revision_score": 0.60,
            "eps_revision_7d": 0.20,
            "eps_revision_30d": 0.20,
        },
        "order_flow": {
            "recent_contract_count": 0.18,
            "recent_contract_amount": 0.22,
            "backlog_amount": 0.24,
            "contract_liability": 0.14,
            "has_major_contract": 0.12,
            "has_bid_award": 0.10,
        },
        "event_drive": {
            "fact_contract_count": 0.25,
            "fact_contract_amount": 0.25,
            "fact_government_contracts": 0.10,
            "positive_supply_signals": 0.25,
            "total_supply_signals": 0.05,
            "negative_supply_signals_inverted": 0.10,
        },
        "industry": {
            "qianzhan_relevance": 0.45,
            "qianzhan_direction_bias": 0.20,
            "inventory_direction_bias": 0.20,
            "operation_direction_bias": 0.15,
        },
        "liquidity": {
            "turnover_pct_rank": 0.34,
            "crowding_score_inverted": 0.22,
            "fund_exposure_proxy": 0.18,
            "northbound_holding_change": 0.13,
            "margin_balance_change": 0.13,
        },
    }
    return {name: _weight_map(cfg.get("signal_weights", {}) or {}, name, values) for name, values in defaults.items()}


def _meta_weights(cfg: Dict[str, Any]) -> Dict[str, float]:
    return _weight_map(
        cfg,
        "meta_weights",
        {
            "valuation_signal_score": 0.14,
            "revision_signal_score": 0.20,
            "order_flow_signal_score": 0.23,
            "event_drive_signal_score": 0.21,
            "industry_signal_score": 0.12,
            "liquidity_signal_score": 0.10,
        },
    )


def _priority_weights(cfg: Dict[str, Any]) -> Dict[str, float]:
    raw = dict(cfg.get("priority_weights", {}) or {})
    return {
        "selection_score_base": max(0.0, _safe_float(raw.get("selection_score_base"), 0.66)),
        "selection_score_risk_budget_bonus": max(0.0, _safe_float(raw.get("selection_score_risk_budget_bonus"), 0.12)),
        "data_activation_score": max(0.0, _safe_float(raw.get("data_activation_score"), 0.34)),
    }


def _llm_overlay_cfg(cfg: Dict[str, Any]) -> Dict[str, float]:
    raw = dict(cfg.get("llm_overlay", {}) or {})
    return {
        "favored_symbol_boost": max(0.0, _safe_float(raw.get("favored_symbol_boost"), 0.09)),
        "favored_family_boost": max(0.0, _safe_float(raw.get("favored_family_boost"), 0.05)),
        "blocked_symbol_penalty": max(0.0, _safe_float(raw.get("blocked_symbol_penalty"), 0.16)),
        "aggressiveness_min": _safe_float(raw.get("aggressiveness_min"), 0.85),
        "aggressiveness_max": _safe_float(raw.get("aggressiveness_max"), 1.35),
    }


def _risk_budget_cfg(cfg: Dict[str, Any]) -> Dict[str, float]:
    raw = dict(cfg.get("risk_budget_bounds", {}) or {})
    return {
        "min": _safe_float(raw.get("min"), 0.35),
        "max": _safe_float(raw.get("max"), 1.6),
        "priority_bonus_cap": _safe_float(raw.get("priority_bonus_cap"), 1.0),
    }


def _cash_multiplier_cfg(cfg: Dict[str, Any]) -> Dict[str, float]:
    raw = dict(cfg.get("cash_multipliers", {}) or {})
    return {
        "high_cash_threshold": _safe_float(raw.get("high_cash_threshold"), 0.15),
        "mid_cash_threshold": _safe_float(raw.get("mid_cash_threshold"), 0.08),
        "high_cash_multiplier": _safe_float(raw.get("high_cash_multiplier"), 1.0),
        "mid_cash_multiplier": _safe_float(raw.get("mid_cash_multiplier"), 0.94),
        "low_cash_multiplier": _safe_float(raw.get("low_cash_multiplier"), 0.88),
    }


def _sqlite_path(config: Dict[str, Any], key: str, fallback_name: str) -> Path:
    raw = _text(dict(config.get("paths", {}) or {}).get(key))
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parents[3] / "data" / "sql_store" / fallback_name).resolve()


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def _query_frame(db_path: Path, sql: str, params: Sequence[Any]) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return pd.read_sql_query(sql, conn, params=list(params))
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()


def _industry_column(df: pd.DataFrame) -> str:
    for column in ("industry", "industry_name", "router_industry_bucket", "sector"):
        if column in df.columns:
            return column
    return ""


def _load_runtime_stock_features(db_path: Path, codes: Sequence[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["code"])
    placeholders = _placeholders(codes)
    valuation_sql = f"""
    WITH ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
        FROM valuation_daily
        WHERE stock_code IN ({placeholders})
    )
    SELECT
        stock_code AS code,
        trade_date AS valuation_trade_date,
        pe_pct_1y,
        pb_pct_1y,
        ps_pct_1y,
        pe_pct_industry,
        pb_pct_industry,
        ps_pct_industry
    FROM ranked
    WHERE rn = 1
    """
    crowding_sql = f"""
    WITH ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
        FROM crowding_daily
        WHERE stock_code IN ({placeholders})
    )
    SELECT
        stock_code AS code,
        trade_date AS crowding_trade_date,
        turnover_pct_rank,
        crowding_score,
        fund_exposure_proxy,
        northbound_holding_change,
        margin_balance_change
    FROM ranked
    WHERE rn = 1
    """
    revision_sql = f"""
    WITH ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
        FROM expectation_revision_daily
        WHERE stock_code IN ({placeholders})
    )
    SELECT
        stock_code AS code,
        trade_date AS revision_trade_date,
        eps_revision_7d,
        eps_revision_30d,
        analyst_count,
        revision_score
    FROM ranked
    WHERE rn = 1
    """
    contract_sql = f"""
    SELECT
        stock_code AS code,
        COUNT(*) AS recent_contract_count,
        SUM(COALESCE(contract_amount, 0.0)) AS recent_contract_amount,
        MAX(CASE WHEN COALESCE(is_major_contract, 0) > 0 THEN 1 ELSE 0 END) AS has_major_contract,
        AVG(COALESCE(parse_confidence, 0.0)) AS contract_parse_confidence
    FROM company_contract_fact
    WHERE stock_code IN ({placeholders})
      AND date(COALESCE(announcement_date, created_at)) >= date('now', '-30 day')
    GROUP BY stock_code
    """
    backlog_sql = f"""
    WITH ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY stock_code
                   ORDER BY COALESCE(announcement_date, period, created_at) DESC
               ) AS rn
        FROM company_order_backlog_fact
        WHERE stock_code IN ({placeholders})
    )
    SELECT
        stock_code AS code,
        COALESCE(backlog_amount, 0.0) AS backlog_amount,
        COALESCE(contract_liability, 0.0) AS contract_liability,
        COALESCE(prepayment, 0.0) AS prepayment,
        COALESCE(capex, 0.0) AS capex
    FROM ranked
    WHERE rn = 1
    """
    merged = pd.DataFrame({"code": list(codes)})
    for sql in (valuation_sql, crowding_sql, revision_sql, contract_sql, backlog_sql):
        frame = _query_frame(db_path, sql, codes)
        if frame.empty:
            continue
        merged = merged.merge(frame, on="code", how="left")
    return merged


def _load_fact_stock_features(db_path: Path, symbols: Sequence[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["ts_code"])
    placeholders = _placeholders(symbols)
    contract_sql = f"""
    SELECT
        symbol AS ts_code,
        COUNT(*) AS fact_contract_count,
        SUM(COALESCE(amount_cny, 0.0)) AS fact_contract_amount,
        SUM(CASE WHEN COALESCE(counterparty_is_government, 0) > 0 THEN 1 ELSE 0 END) AS fact_government_contracts,
        MAX(CASE WHEN COALESCE(is_bid_award, 0) > 0 THEN 1 ELSE 0 END) AS has_bid_award,
        MAX(CASE WHEN COALESCE(is_new_order, 0) > 0 THEN 1 ELSE 0 END) AS has_new_order
    FROM event_fact_contract_orders
    WHERE symbol IN ({placeholders})
      AND date(COALESCE(event_date, trade_date)) >= date('now', '-14 day')
    GROUP BY symbol
    """
    supply_sql = f"""
    SELECT
        symbol AS ts_code,
        SUM(CASE
                WHEN lower(COALESCE(direction, '')) IN ('positive', 'up', 'increase', 'bullish', 'tightening')
                THEN 1 ELSE 0 END) AS positive_supply_signals,
        SUM(CASE
                WHEN lower(COALESCE(direction, '')) IN ('negative', 'down', 'decrease', 'bearish', 'weak', 'loosening')
                THEN 1 ELSE 0 END) AS negative_supply_signals,
        COUNT(*) AS total_supply_signals
    FROM event_fact_supply_chain_signals
    WHERE symbol IN ({placeholders})
      AND date(COALESCE(event_date, trade_date)) >= date('now', '-14 day')
    GROUP BY symbol
    """
    merged = pd.DataFrame({"ts_code": list(symbols)})
    for sql in (contract_sql, supply_sql):
        frame = _query_frame(db_path, sql, symbols)
        if frame.empty:
            continue
        merged = merged.merge(frame, on="ts_code", how="left")
    return merged


def _load_industry_features(db_path: Path, industries: Sequence[str]) -> pd.DataFrame:
    clean = [item for item in industries if item]
    if not clean:
        return pd.DataFrame(columns=["industry_key"])
    placeholders = _placeholders(clean)
    qianzhan_sql = f"""
    SELECT
        industry_name AS industry_key,
        AVG(COALESCE(llm_relevance_score, 0.0)) AS qianzhan_relevance,
        AVG(CASE
                WHEN lower(COALESCE(direction_hint, '')) IN ('positive', 'up', 'increase', 'bullish', 'tightening')
                THEN 1.0
                WHEN lower(COALESCE(direction_hint, '')) IN ('negative', 'down', 'decrease', 'bearish', 'weak', 'loosening')
                THEN -1.0
                ELSE 0.0
            END) AS qianzhan_direction_bias,
        COUNT(*) AS qianzhan_item_count
    FROM qianzhan_indicator_daily
    WHERE industry_name IN ({placeholders})
      AND date(COALESCE(publish_date, trade_date)) >= date('now', '-21 day')
    GROUP BY industry_name
    """
    inventory_sql = f"""
    SELECT
        industry_name AS industry_key,
        AVG(CASE
                WHEN lower(COALESCE(direction_hint, '')) IN ('positive', 'up', 'increase', 'bullish', 'tightening')
                THEN 1.0
                WHEN lower(COALESCE(direction_hint, '')) IN ('negative', 'down', 'decrease', 'bearish', 'weak', 'loosening')
                THEN -1.0
                ELSE 0.0
            END) AS inventory_direction_bias,
        COUNT(*) AS inventory_factor_count
    FROM industry_factor_price_inventory_daily
    WHERE industry_name IN ({placeholders})
      AND date(COALESCE(publish_date, trade_date)) >= date('now', '-21 day')
    GROUP BY industry_name
    """
    operation_sql = f"""
    SELECT
        industry_name AS industry_key,
        AVG(CASE
                WHEN lower(COALESCE(direction_hint, '')) IN ('positive', 'up', 'increase', 'bullish', 'tightening')
                THEN 1.0
                WHEN lower(COALESCE(direction_hint, '')) IN ('negative', 'down', 'decrease', 'bearish', 'weak', 'loosening')
                THEN -1.0
                ELSE 0.0
            END) AS operation_direction_bias,
        COUNT(*) AS operation_factor_count
    FROM industry_factor_operation_daily
    WHERE industry_name IN ({placeholders})
      AND date(COALESCE(publish_date, trade_date)) >= date('now', '-21 day')
    GROUP BY industry_name
    """
    merged = pd.DataFrame({"industry_key": list(clean)})
    for sql in (qianzhan_sql, inventory_sql, operation_sql):
        frame = _query_frame(db_path, sql, clean)
        if frame.empty:
            continue
        merged = merged.merge(frame, on="industry_key", how="left")
    return merged


def _candidate_rows(df: pd.DataFrame, limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for _, row in df.head(max(int(limit or 0), 1)).iterrows():
        rows.append(
            {
                "symbol": _text(row.get("ts_code")).upper(),
                "industry": _text(row.get("industry")),
                "selection_score": round(_safe_float(row.get("selection_score")), 6),
                "data_activation_score": round(_safe_float(row.get("data_activation_score")), 6),
                "alpha_activation_priority": round(_safe_float(row.get("alpha_activation_priority")), 6),
                "alpha_family": _text(row.get("activation_alpha_family")),
                "valuation_score": round(_safe_float(row.get("valuation_signal_score")), 6),
                "revision_score": round(_safe_float(row.get("revision_signal_score")), 6),
                "order_score": round(_safe_float(row.get("order_flow_signal_score")), 6),
                "industry_score": round(_safe_float(row.get("industry_signal_score")), 6),
                "event_score": round(_safe_float(row.get("event_drive_signal_score")), 6),
                "liquidity_score": round(_safe_float(row.get("liquidity_signal_score")), 6),
                "integrated_thesis_state": _text(row.get("integrated_thesis_state")),
                "primary_event_type": _text(row.get("primary_event_type")),
                "primary_mechanism_group": _text(row.get("primary_mechanism_group")),
            }
        )
    return rows


def _default_llm_review(reason: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "review_summary": reason,
        "favored_symbols": [],
        "blocked_symbols": [],
        "favored_alpha_families": [],
        "aggressiveness_multiplier": 1.0,
        "risk_flags": ["strategy_activation_llm_unavailable"],
        "decision_basis": [],
        "uncertainty_flags": ["llm_unavailable"],
        "overfit_guard": "fallback_to_quant_scores_only",
        "provider": "",
        "model": "",
        "error": reason,
    }


def _build_llm_prompts(
    *,
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    candidate_rows: List[Dict[str, Any]],
    llm_overlay_cfg: Dict[str, float],
) -> Dict[str, str]:
    system_prompt = (
        "You are the outer allocator for an A-share multi-alpha system. "
        "You do not invent symbols and you do not hard-block the portfolio. "
        "Use the candidate data activation signals to widen and reprioritize the pool. "
        "Return strict JSON only."
    )
    payload = {
        "task": "Review the candidate pool after SQL fact activation and return an aggressive but evidence-based ranking overlay.",
        "required_json_schema": {
            "review_summary": "string",
            "favored_symbols": ["string"],
            "blocked_symbols": ["string"],
            "favored_alpha_families": ["valuation|revision|order_flow|event_drive|industry|liquidity"],
            "aggressiveness_multiplier": "float",
            "risk_flags": ["string"],
            "decision_basis": ["string"],
            "uncertainty_flags": ["string"],
            "overfit_guard": "string",
        },
        "rules": [
            "Do not output symbols not present in the input.",
            "Keep favored_symbols <= 6 and blocked_symbols <= 4.",
            (
                "aggressiveness_multiplier must be between "
                f"{float(llm_overlay_cfg.get('aggressiveness_min', 0.85)):.2f} and "
                f"{float(llm_overlay_cfg.get('aggressiveness_max', 1.35)):.2f}."
            ),
            "Prefer widening around strong revisions, order flow, industry acceleration, and event follow-through.",
        ],
        "market_state": {
            "market_regime": _text(market_state.get("market_regime")),
            "style_bias": _text(market_state.get("style_bias")),
            "mechanism_bias": _text(market_state.get("mechanism_bias")),
            "risk_budget_multiplier": _safe_float(market_state.get("risk_budget_multiplier"), 1.0),
        },
        "account_state": {
            "nav": _safe_float(account_ctx.get("nav")),
            "cash": _safe_float(account_ctx.get("cash")),
            "positions_count": int(account_ctx.get("positions_count") or 0),
        },
        "candidates": candidate_rows,
    }
    return {"system_prompt": system_prompt, "user_prompt": json.dumps(payload, ensure_ascii=False)}


def _review_activation_pool(
    *,
    config: Dict[str, Any],
    cfg: Dict[str, Any],
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    candidate_df: pd.DataFrame,
) -> Dict[str, Any]:
    if not bool(cfg.get("llm_enabled", True)):
        return {"enabled": False, "applied": False}
    overlay_cfg = _llm_overlay_cfg(cfg)
    candidate_rows = _candidate_rows(candidate_df, limit=int(cfg.get("llm_max_input_rows", 16) or 16))
    allowed_symbols = {item["symbol"] for item in candidate_rows if item.get("symbol")}
    prompts = _build_llm_prompts(
        market_state=market_state,
        account_ctx=account_ctx,
        candidate_rows=candidate_rows,
        llm_overlay_cfg=overlay_cfg,
    )
    provider = _text(cfg.get("llm_provider") or "deepseek_worker") or "deepseek_worker"
    providers_cfg = dict(config.get("providers", {}) or {})
    timeout_seconds = int(cfg.get("llm_timeout_seconds", 40) or 40)
    if provider == "local_ollama":
        client = LocalOllamaChatClient(provider_name="strategy_activation_ollama", cfg=dict(config.get("local_ollama", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    elif provider == "openai_research":
        client = OpenAIResponsesClient(provider_name="strategy_activation_openai", cfg=dict(providers_cfg.get("openai_research", {}) or {}))
        result = client.create_json_response_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    else:
        client = DeepSeekChatClient(provider_name="strategy_activation_deepseek", cfg=dict(providers_cfg.get("deepseek_worker", {}) or {}))
        result = client.chat_json_detailed(prompts["system_prompt"], prompts["user_prompt"], timeout_seconds=timeout_seconds)
    if not bool(result.get("ok", False)):
        normalized = _default_llm_review(_text(result.get("error_message")) or "strategy_activation_llm_failed")
    else:
        raw = dict(result.get("data", {}) or {})
        favored_symbols: List[str] = []
        for item in list(raw.get("favored_symbols", []) or []):
            symbol = _normalize_symbol(item).upper()
            if symbol and symbol in allowed_symbols and symbol not in favored_symbols:
                favored_symbols.append(symbol)
        blocked_symbols: List[str] = []
        for item in list(raw.get("blocked_symbols", []) or []):
            symbol = _normalize_symbol(item).upper()
            if symbol and symbol in allowed_symbols and symbol not in blocked_symbols:
                blocked_symbols.append(symbol)
        favored_families: List[str] = []
        for item in list(raw.get("favored_alpha_families", []) or []):
            family = _text(item).lower()
            if family and family in {"valuation", "revision", "order_flow", "event_drive", "industry", "liquidity"} and family not in favored_families:
                favored_families.append(family)
        normalized = {
            "ok": True,
            "review_summary": _text(raw.get("review_summary")),
            "favored_symbols": favored_symbols[:6],
            "blocked_symbols": blocked_symbols[:4],
            "favored_alpha_families": favored_families[:3],
            "aggressiveness_multiplier": max(
                _safe_float(overlay_cfg.get("aggressiveness_min"), 0.85),
                min(
                    _safe_float(overlay_cfg.get("aggressiveness_max"), 1.35),
                    _safe_float(raw.get("aggressiveness_multiplier"), 1.0),
                ),
            ),
            "risk_flags": [_text(item) for item in list(raw.get("risk_flags", []) or []) if _text(item)][:6],
            "decision_basis": [_text(item) for item in list(raw.get("decision_basis", []) or []) if _text(item)][:4],
            "uncertainty_flags": [_text(item) for item in list(raw.get("uncertainty_flags", []) or []) if _text(item)][:6],
            "overfit_guard": _text(raw.get("overfit_guard"))[:240],
            "provider": _text(result.get("provider")),
            "model": _text(result.get("model")),
            "elapsed_seconds": _safe_float(result.get("elapsed_seconds")),
            "error": "",
        }
    artifact_root = ensure_dir(Path(_text(cfg.get("artifact_root")) or (Path(__file__).resolve().parents[3] / "data" / "trade_clock" / "strategy_activation_llm")).resolve())
    latest_root = ensure_dir(artifact_root / "latest")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": int(len(candidate_df.index)),
        "input_sample": candidate_rows,
        "review": normalized,
        "raw_model_result": {
            "ok": bool(result.get("ok", False)) if "result" in locals() else False,
            "provider": _text(result.get("provider")) if "result" in locals() else "",
            "model": _text(result.get("model")) if "result" in locals() else "",
            "error_type": _text(result.get("error_type")) if "result" in locals() else "",
            "error_message": _text(result.get("error_message")) if "result" in locals() else "",
            "elapsed_seconds": _safe_float(result.get("elapsed_seconds")) if "result" in locals() else 0.0,
        },
        "enabled": True,
        "applied": bool(normalized.get("ok", False)),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact_path = artifact_root / f"strategy_activation_llm_{stamp}.json"
    latest_path = latest_root / "latest_strategy_activation_llm.json"
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["trace"] = write_llm_trace(
        config=config,
        stage="strategy_activation",
        provider=provider,
        system_prompt=prompts["system_prompt"],
        user_prompt=prompts["user_prompt"],
        result=result if "result" in locals() else {},
        normalized_review=normalized,
        input_payload={"candidate_rows": candidate_rows},
        meta={"candidate_count": int(len(candidate_df.index))},
    )
    payload["artifact_path"] = str(artifact_path)
    payload["latest_artifact_path"] = str(latest_path)
    return payload


def activate_candidate_pool(
    *,
    candidate_df: pd.DataFrame,
    config: Dict[str, Any],
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    cfg = dict(config.get("strategy_activation", {}) or {})
    if candidate_df.empty or not bool(cfg.get("enabled", True)):
        return candidate_df.copy(), {"enabled": bool(cfg.get("enabled", True)), "applied": False}
    out = candidate_df.copy().reset_index(drop=True)
    if "ts_code" not in out.columns and "code" in out.columns:
        out["ts_code"] = out["code"].map(_normalize_symbol)
    out["ts_code"] = out["ts_code"].map(_normalize_symbol)
    out["code"] = out["ts_code"].map(_ts_to_code)
    industry_col = _industry_column(out)
    if industry_col and industry_col != "industry":
        out["industry"] = out[industry_col].astype(str).str.strip()
    elif "industry" not in out.columns:
        out["industry"] = ""

    codes = [item for item in out["code"].dropna().astype(str).map(str.strip).tolist() if item]
    symbols = [item for item in out["ts_code"].dropna().astype(str).map(str.strip).tolist() if item]
    industries = [item for item in out["industry"].dropna().astype(str).map(str.strip).tolist() if item]

    activation_feature_columns = [
        "valuation_trade_date",
        "pe_pct_1y",
        "pb_pct_1y",
        "ps_pct_1y",
        "pe_pct_industry",
        "pb_pct_industry",
        "ps_pct_industry",
        "crowding_trade_date",
        "turnover_pct_rank",
        "crowding_score",
        "fund_exposure_proxy",
        "northbound_holding_change",
        "margin_balance_change",
        "revision_trade_date",
        "eps_revision_7d",
        "eps_revision_30d",
        "analyst_count",
        "revision_score",
        "recent_contract_count",
        "recent_contract_amount",
        "has_major_contract",
        "contract_parse_confidence",
        "backlog_amount",
        "contract_liability",
        "prepayment",
        "capex",
        "fact_contract_count",
        "fact_contract_amount",
        "fact_government_contracts",
        "has_bid_award",
        "has_new_order",
        "positive_supply_signals",
        "negative_supply_signals",
        "total_supply_signals",
        "qianzhan_relevance",
        "qianzhan_direction_bias",
        "qianzhan_item_count",
        "inventory_direction_bias",
        "inventory_factor_count",
        "operation_direction_bias",
        "operation_factor_count",
        "industry_key",
    ]
    out = out.drop(columns=[column for column in activation_feature_columns if column in out.columns], errors="ignore")

    runtime_db = _sqlite_path(config, "data_sqlite_path", "research_data_v1.sqlite3")
    fact_db = _sqlite_path(config, "research_fact_sqlite_path", "research_fact_layers_v1.sqlite3")
    runtime_features = _load_runtime_stock_features(runtime_db, codes)
    fact_features = _load_fact_stock_features(fact_db, symbols)
    industry_features = _load_industry_features(fact_db, sorted(set(industries)))

    if not runtime_features.empty:
        out = out.merge(runtime_features, on="code", how="left")
    if not fact_features.empty:
        out = out.merge(fact_features, on="ts_code", how="left")
    if not industry_features.empty:
        out = out.merge(industry_features, left_on="industry", right_on="industry_key", how="left")
    signal_weights = _signal_weight_sets(cfg)
    meta_weights = _meta_weights(cfg)
    priority_weights = _priority_weights(cfg)
    overlay_cfg = _llm_overlay_cfg(cfg)
    risk_budget_cfg = _risk_budget_cfg(cfg)
    cash_cfg = _cash_multiplier_cfg(cfg)

    out["valuation_signal_score"] = (
        _normalize_series(out, "pe_pct_industry", invert=True) * signal_weights["valuation"]["pe_pct_industry"]
        + _normalize_series(out, "pb_pct_industry", invert=True) * signal_weights["valuation"]["pb_pct_industry"]
        + _normalize_series(out, "ps_pct_industry", invert=True) * signal_weights["valuation"]["ps_pct_industry"]
    )
    out["revision_signal_score"] = (
        _normalize_series(out, "revision_score") * signal_weights["revision"]["revision_score"]
        + _normalize_series(out, "eps_revision_7d") * signal_weights["revision"]["eps_revision_7d"]
        + _normalize_series(out, "eps_revision_30d") * signal_weights["revision"]["eps_revision_30d"]
    )
    out["order_flow_signal_score"] = (
        _normalize_series(out, "recent_contract_count") * signal_weights["order_flow"]["recent_contract_count"]
        + _normalize_series(out, "recent_contract_amount") * signal_weights["order_flow"]["recent_contract_amount"]
        + _normalize_series(out, "backlog_amount") * signal_weights["order_flow"]["backlog_amount"]
        + _normalize_series(out, "contract_liability") * signal_weights["order_flow"]["contract_liability"]
        + _numeric_col(out, "has_major_contract").clip(lower=0.0, upper=1.0) * signal_weights["order_flow"]["has_major_contract"]
        + _numeric_col(out, "has_bid_award").clip(lower=0.0, upper=1.0) * signal_weights["order_flow"]["has_bid_award"]
    )
    out["event_drive_signal_score"] = (
        _normalize_series(out, "fact_contract_count") * signal_weights["event_drive"]["fact_contract_count"]
        + _normalize_series(out, "fact_contract_amount") * signal_weights["event_drive"]["fact_contract_amount"]
        + _normalize_series(out, "fact_government_contracts") * signal_weights["event_drive"]["fact_government_contracts"]
        + _normalize_series(out, "positive_supply_signals") * signal_weights["event_drive"]["positive_supply_signals"]
        + _normalize_series(out, "total_supply_signals") * signal_weights["event_drive"]["total_supply_signals"]
        + _normalize_series(out, "negative_supply_signals", invert=True) * signal_weights["event_drive"]["negative_supply_signals_inverted"]
    )
    out["industry_signal_score"] = (
        _normalize_series(out, "qianzhan_relevance") * signal_weights["industry"]["qianzhan_relevance"]
        + _normalize_series(out, "qianzhan_direction_bias") * signal_weights["industry"]["qianzhan_direction_bias"]
        + _normalize_series(out, "inventory_direction_bias") * signal_weights["industry"]["inventory_direction_bias"]
        + _normalize_series(out, "operation_direction_bias") * signal_weights["industry"]["operation_direction_bias"]
    )
    out["liquidity_signal_score"] = (
        _normalize_series(out, "turnover_pct_rank") * signal_weights["liquidity"]["turnover_pct_rank"]
        + _normalize_series(out, "crowding_score", invert=True) * signal_weights["liquidity"]["crowding_score_inverted"]
        + _normalize_series(out, "fund_exposure_proxy") * signal_weights["liquidity"]["fund_exposure_proxy"]
        + _normalize_series(out, "northbound_holding_change") * signal_weights["liquidity"]["northbound_holding_change"]
        + _normalize_series(out, "margin_balance_change") * signal_weights["liquidity"]["margin_balance_change"]
    )
    out["data_activation_score"] = (
        out["valuation_signal_score"] * meta_weights["valuation_signal_score"]
        + out["revision_signal_score"] * meta_weights["revision_signal_score"]
        + out["order_flow_signal_score"] * meta_weights["order_flow_signal_score"]
        + out["event_drive_signal_score"] * meta_weights["event_drive_signal_score"]
        + out["industry_signal_score"] * meta_weights["industry_signal_score"]
        + out["liquidity_signal_score"] * meta_weights["liquidity_signal_score"]
    ).clip(lower=0.0, upper=1.0)

    families = {
        "valuation": out["valuation_signal_score"],
        "revision": out["revision_signal_score"],
        "order_flow": out["order_flow_signal_score"],
        "event_drive": out["event_drive_signal_score"],
        "industry": out["industry_signal_score"],
        "liquidity": out["liquidity_signal_score"],
    }
    family_frame = pd.DataFrame(families)
    family_max = family_frame.max(axis=1)
    out["activation_alpha_family"] = family_frame.idxmax(axis=1)
    out.loc[family_max <= 1e-9, "activation_alpha_family"] = "unclassified"
    blend_weight = max(0.0, min(0.65, _safe_float(cfg.get("blend_weight"), 0.32)))
    out["selection_score"] = (
        _numeric_col(out, "selection_score") * (1.0 - blend_weight)
        + out["data_activation_score"] * blend_weight
    )
    out = enrich_alpha_registry(out)

    llm_review = _review_activation_pool(
        config=config,
        cfg=cfg,
        market_state=market_state,
        account_ctx=account_ctx,
        candidate_df=out.sort_values(["selection_score", "data_activation_score"], ascending=[False, False]).reset_index(drop=True),
    )
    review = dict(llm_review.get("review", {}) or {})
    favored_symbols = {item for item in list(review.get("favored_symbols", []) or []) if item}
    blocked_symbols = {item for item in list(review.get("blocked_symbols", []) or []) if item}
    favored_families = {item for item in list(review.get("favored_alpha_families", []) or []) if item}
    out["activation_llm_boost"] = 0.0
    out.loc[out["ts_code"].isin(favored_symbols), "activation_llm_boost"] += overlay_cfg["favored_symbol_boost"]
    out.loc[out["activation_alpha_family"].isin(favored_families), "activation_llm_boost"] += overlay_cfg["favored_family_boost"]
    out.loc[out["ts_code"].isin(blocked_symbols), "activation_llm_boost"] -= overlay_cfg["blocked_symbol_penalty"]
    aggressiveness_multiplier = max(
        overlay_cfg["aggressiveness_min"],
        min(overlay_cfg["aggressiveness_max"], _safe_float(review.get("aggressiveness_multiplier"), 1.0)),
    )
    risk_budget = max(
        risk_budget_cfg["min"],
        min(risk_budget_cfg["max"], _safe_float(market_state.get("risk_budget_multiplier"), 1.0)),
    )
    cash_ratio = 0.0
    if _safe_float(account_ctx.get("nav")) > 0:
        cash_ratio = _safe_float(account_ctx.get("cash")) / max(_safe_float(account_ctx.get("nav")), 1e-9)
    cash_multiplier = (
        cash_cfg["high_cash_multiplier"]
        if cash_ratio >= cash_cfg["high_cash_threshold"]
        else cash_cfg["mid_cash_multiplier"]
        if cash_ratio >= cash_cfg["mid_cash_threshold"]
        else cash_cfg["low_cash_multiplier"]
    )
    out["alpha_activation_priority"] = (
        out["selection_score"]
        * (
            priority_weights["selection_score_base"]
            + priority_weights["selection_score_risk_budget_bonus"] * min(risk_budget, risk_budget_cfg["priority_bonus_cap"])
        )
        + out["data_activation_score"] * priority_weights["data_activation_score"]
        + out["activation_llm_boost"]
    ) * aggressiveness_multiplier * cash_multiplier

    coverage_columns = [
        "pe_pct_industry",
        "revision_score",
        "recent_contract_count",
        "backlog_amount",
        "fact_contract_count",
        "positive_supply_signals",
        "qianzhan_relevance",
        "turnover_pct_rank",
    ]
    summary = {
        "enabled": True,
        "applied": True,
        "runtime_sqlite_path": str(runtime_db),
        "fact_sqlite_path": str(fact_db),
        "candidate_count": int(len(out.index)),
        "feature_coverage": {
            column: int(pd.to_numeric(out.get(column), errors="coerce").notna().sum()) if column in out.columns else 0
            for column in coverage_columns
        },
        "alpha_family_counts": {
            str(key): int(value)
            for key, value in out["activation_alpha_family"].value_counts().to_dict().items()
        },
        "alpha_family_score_means": {
            "valuation": round(float(out["valuation_signal_score"].mean()), 6),
            "revision": round(float(out["revision_signal_score"].mean()), 6),
            "order_flow": round(float(out["order_flow_signal_score"].mean()), 6),
            "event_drive": round(float(out["event_drive_signal_score"].mean()), 6),
            "industry": round(float(out["industry_signal_score"].mean()), 6),
            "liquidity": round(float(out["liquidity_signal_score"].mean()), 6),
        },
        "alpha_family_data_map": {
            "event_drive": ["event_fact_contract_orders", "event_fact_supply_chain_signals"],
            "order_flow": ["company_contract_fact", "company_order_backlog_fact", "event_fact_contract_orders"],
            "revision": ["expectation_revision_daily"],
            "industry": ["qianzhan_indicator_daily", "industry_factor_price_inventory_daily", "industry_factor_operation_daily"],
            "valuation": ["valuation_daily"],
            "liquidity": ["crowding_daily"],
        },
        "alpha_registry": summarize_alpha_registry(out),
        "llm_review": llm_review,
        "blend_weight": blend_weight,
        "risk_budget_multiplier": risk_budget,
        "cash_multiplier": cash_multiplier,
        "weights": {
            "signal_weights": signal_weights,
            "meta_weights": meta_weights,
            "priority_weights": priority_weights,
            "llm_overlay": overlay_cfg,
            "risk_budget_bounds": risk_budget_cfg,
            "cash_multipliers": cash_cfg,
        },
    }
    return out, summary
