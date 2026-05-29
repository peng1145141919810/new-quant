from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


ALPHA_REGISTRY: List[Dict[str, Any]] = [
    {"family": "event_drive", "horizon": "swing", "capacity": "mid", "style": "aggressive", "objective": "event_follow_through", "risk_budget": "high", "default_state": "live", "data_sources": ["exchange_disclosure", "event_fact_contract_orders", "event_fact_supply_chain_signals"], "score_columns": ["event_drive_signal_score"], "allocator_priority": 1.16},
    {"family": "order_flow", "horizon": "short", "capacity": "mid", "style": "aggressive", "objective": "contract_and_order_acceleration", "risk_budget": "high", "default_state": "live", "data_sources": ["company_contract_fact", "company_order_backlog_fact", "event_fact_contract_orders"], "score_columns": ["order_flow_signal_score"], "allocator_priority": 1.12},
    {"family": "revision", "horizon": "swing", "capacity": "mid", "style": "balanced", "objective": "expectation_repricing", "risk_budget": "medium", "default_state": "live", "data_sources": ["expectation_revision_daily", "exchange_disclosure"], "score_columns": ["revision_signal_score"], "allocator_priority": 1.04},
    {"family": "industry", "horizon": "swing", "capacity": "high", "style": "balanced", "objective": "industry_diffusion", "risk_budget": "medium", "default_state": "shadow", "data_sources": ["qianzhan_indicator_daily", "industry_factor_price_inventory_daily", "industry_factor_operation_daily"], "score_columns": ["industry_signal_score"], "allocator_priority": 0.94},
    {"family": "valuation", "horizon": "medium", "capacity": "high", "style": "balanced", "objective": "valuation_reversion", "risk_budget": "medium", "default_state": "shadow", "data_sources": ["valuation_daily"], "score_columns": ["valuation_signal_score"], "allocator_priority": 0.92},
    {"family": "liquidity", "horizon": "short", "capacity": "low", "style": "tactical", "objective": "intraday_liquidity_timing", "risk_budget": "low", "default_state": "shadow", "data_sources": ["crowding_daily", "intraday_proxy_stream"], "score_columns": ["liquidity_signal_score"], "allocator_priority": 0.84},
]


def enrich_alpha_registry(candidate_df: pd.DataFrame) -> pd.DataFrame:
    out = candidate_df.copy()
    if out.empty:
        return out
    if "activation_alpha_family" not in out.columns:
        out["activation_alpha_family"] = "event_drive"
    registry_map = {str(item["family"]): dict(item) for item in ALPHA_REGISTRY}
    out["alpha_family_horizon"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("horizon", "unknown"))
    out["alpha_family_capacity"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("capacity", "unknown"))
    out["alpha_family_style"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("style", "unknown"))
    out["alpha_family_objective"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("objective", "unknown"))
    out["alpha_family_risk_budget"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("risk_budget", "unknown"))
    out["alpha_family_default_state"] = out["activation_alpha_family"].map(lambda x: registry_map.get(str(x), {}).get("default_state", "shadow"))
    return out


def summarize_alpha_registry(candidate_df: pd.DataFrame) -> Dict[str, Any]:
    if candidate_df is None or candidate_df.empty:
        return {"enabled": True, "families": [], "family_counts": {}, "family_weight_proxy": {}, "family_score_means": {}}
    family_col = candidate_df.get("activation_alpha_family", pd.Series(dtype="object")).astype(str)
    weight_col = pd.to_numeric(candidate_df.get("portfolio_weight"), errors="coerce").fillna(0.0)
    family_counts = {str(k): int(v) for k, v in family_col.value_counts().to_dict().items()}
    family_weight_proxy = {
        str(k): round(float(v), 6)
        for k, v in candidate_df.assign(_family=family_col, _weight=weight_col).groupby("_family")["_weight"].sum().to_dict().items()
    }
    family_score_means = {}
    if "alpha_activation_priority" in candidate_df.columns:
        family_score_means = {
            str(k): round(float(v), 6)
            for k, v in candidate_df.assign(_family=family_col, _priority=pd.to_numeric(candidate_df.get("alpha_activation_priority"), errors="coerce").fillna(0.0)).groupby("_family")["_priority"].mean().to_dict().items()
        }
    return {
        "enabled": True,
        "families": [dict(item) for item in ALPHA_REGISTRY],
        "family_counts": family_counts,
        "family_weight_proxy": family_weight_proxy,
        "family_score_means": family_score_means,
    }


def family_registry_map() -> Dict[str, Dict[str, Any]]:
    return {str(item["family"]): dict(item) for item in ALPHA_REGISTRY}


def apply_registered_family_budget(
    df: pd.DataFrame,
    *,
    weight_column: str,
    focus_families: List[str] | None = None,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    if out.empty or weight_column not in out.columns:
        return out, {"applied": False, "reason": "missing_weight_column"}
    focus_families = [str(item).strip() for item in list(focus_families or []) if str(item).strip()]
    registry = family_registry_map()
    family_col = out.get("activation_alpha_family", pd.Series(["unknown"] * len(out.index), index=out.index)).fillna("unknown").astype(str)
    base = pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).clip(lower=0.0)
    multipliers = []
    family_multiplier_map: Dict[str, float] = {}
    for family in family_col.tolist():
        row = registry.get(str(family), {})
        mult = float(row.get("allocator_priority", 1.0) or 1.0)
        if str(family) in focus_families:
            mult *= 1.08
        family_multiplier_map[str(family)] = float(mult)
        multipliers.append(mult)
    out[weight_column] = base * pd.Series(multipliers, index=out.index, dtype="float64")
    return out, {
        "applied": True,
        "weight_column": weight_column,
        "focus_families": focus_families,
        "family_multiplier_map": {k: round(v, 6) for k, v in family_multiplier_map.items()},
        "before_total_weight": round(float(base.sum()), 6),
        "after_total_weight": round(float(pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).sum()), 6),
    }
