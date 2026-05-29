from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def _normalize_family(frame: pd.DataFrame) -> pd.Series:
    if "activation_alpha_family" in frame.columns:
        return frame["activation_alpha_family"].fillna("unknown").astype(str)
    if "alpha_family" in frame.columns:
        return frame["alpha_family"].fillna("unknown").astype(str)
    return pd.Series(["unknown"] * len(frame.index), index=frame.index, dtype="object")


def build_alpha_attribution(target_df: pd.DataFrame, position_df: pd.DataFrame) -> Dict[str, Any]:
    if target_df is None:
        target_df = pd.DataFrame()
    if position_df is None:
        position_df = pd.DataFrame()
    target = target_df.copy()
    if not target.empty:
        target["_alpha_family"] = _normalize_family(target)
        target["_weight"] = pd.to_numeric(target.get("portfolio_weight"), errors="coerce").fillna(0.0)
    pos = position_df.copy()
    if not pos.empty:
        pos["_alpha_family"] = _normalize_family(pos)
        pos["_market_value"] = pd.to_numeric(pos.get("market_value"), errors="coerce").fillna(0.0)
        pos["_unrealized_pnl"] = pd.to_numeric(pos.get("unrealized_pnl"), errors="coerce").fillna(0.0)
        pos["_realized_pnl"] = pd.to_numeric(pos.get("realized_pnl"), errors="coerce").fillna(0.0)
        pos["_total_pnl_proxy"] = pos["_unrealized_pnl"] + pos["_realized_pnl"]
    exposure = (
        target.groupby("_alpha_family")["_weight"].sum().sort_values(ascending=False).round(6).to_dict()
        if not target.empty else {}
    )
    pnl_proxy = (
        pos.groupby("_alpha_family")["_total_pnl_proxy"].sum().sort_values(ascending=False).round(4).to_dict()
        if not pos.empty else {}
    )
    market_value = (
        pos.groupby("_alpha_family")["_market_value"].sum().sort_values(ascending=False).round(2).to_dict()
        if not pos.empty else {}
    )
    pnl_yield = {}
    active_symbol_count = {}
    avg_target_weight = {}
    if not pos.empty:
        grouped = pos.groupby("_alpha_family")
        pnl_yield = (
            ((grouped["_total_pnl_proxy"].sum()) / grouped["_market_value"].sum().replace(0.0, pd.NA))
            .fillna(0.0)
            .sort_values(ascending=False)
            .round(6)
            .to_dict()
        )
        active_symbol_count = grouped.size().sort_values(ascending=False).to_dict()
    if not target.empty:
        avg_target_weight = (
            target.groupby("_alpha_family")["_weight"].mean().sort_values(ascending=False).round(6).to_dict()
        )
    return {
        "available": bool(exposure or pnl_proxy or market_value),
        "target_weight_by_alpha_family": {str(k): float(v) for k, v in exposure.items()},
        "pnl_proxy_by_alpha_family": {str(k): float(v) for k, v in pnl_proxy.items()},
        "market_value_by_alpha_family": {str(k): float(v) for k, v in market_value.items()},
        "pnl_yield_by_alpha_family": {str(k): float(v) for k, v in pnl_yield.items()},
        "active_symbol_count_by_alpha_family": {str(k): int(v) for k, v in active_symbol_count.items()},
        "avg_target_weight_by_alpha_family": {str(k): float(v) for k, v in avg_target_weight.items()},
    }
