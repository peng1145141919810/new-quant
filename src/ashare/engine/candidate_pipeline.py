from __future__ import annotations

from typing import Any, Dict, Tuple

import pandas as pd

from .alpha_registry import enrich_alpha_registry, summarize_alpha_registry
from .candidate_pool_llm_review import review_candidate_pool
from .strategy_activation import activate_candidate_pool


def sort_candidate_pool(
    df: pd.DataFrame,
    *,
    include_outer_priority: bool = True,
    include_activation_priority: bool = True,
    include_pred_score: bool = True,
) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    sort_cols = ["selection_score", "integrated_thesis_score", "router_final_score"]
    ascending = [False, False, False]
    if include_pred_score:
        sort_cols.append("pred_score_norm")
        ascending.append(False)
    if include_activation_priority and "alpha_activation_priority" in df.columns:
        sort_cols = ["alpha_activation_priority"] + sort_cols
        ascending = [False] + ascending
    if include_outer_priority and "outer_intelligence_priority" in df.columns:
        sort_cols = ["outer_intelligence_priority"] + sort_cols
        ascending = [False] + ascending
    available_cols = [col for col in sort_cols if col in df.columns]
    available_ascending = [ascending[idx] for idx, col in enumerate(sort_cols) if col in df.columns]
    if not available_cols:
        return df.copy()
    return df.sort_values(by=available_cols, ascending=available_ascending).reset_index(drop=True)


def activate_and_rank_candidate_pool(
    *,
    broad_pool_df: pd.DataFrame,
    config: Dict[str, Any],
    rec_cfg: Dict[str, Any],
    candidate_llm_cfg: Dict[str, Any],
    market_state: Dict[str, Any],
    account_ctx: Dict[str, Any],
    thesis_summary: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    if broad_pool_df.empty:
        disabled = {"enabled": False, "applied": False}
        return broad_pool_df.copy(), disabled, disabled, disabled
    ranked = broad_pool_df.copy()
    ranked, activation_summary = activate_candidate_pool(
        candidate_df=ranked,
        config=config,
        market_state=market_state,
        account_ctx=account_ctx,
    )
    llm_review: Dict[str, Any] = {"enabled": False, "applied": False}
    if bool(candidate_llm_cfg.get("enabled", False)):
        review_rows = sort_candidate_pool(
            ranked,
            include_outer_priority=False,
            include_activation_priority=True,
            include_pred_score=False,
        ).to_dict("records")
        llm_review = review_candidate_pool(
            config=config,
            market_state=market_state,
            account_state=account_ctx,
            thesis_summary=thesis_summary,
            candidate_rows=review_rows,
        )
    # outer_intelligence 账户分档 haircut 已移除：候选排序保持研究/激活原序，
    # 仓位集中度与上限统一交由 decision engine 单遍处理。
    outer_summary: Dict[str, Any] = {"enabled": False, "applied": False, "reason": "outer_intelligence_removed"}
    ranked = enrich_alpha_registry(ranked)
    ranked = sort_candidate_pool(
        ranked,
        include_outer_priority=True,
        include_activation_priority=True,
        include_pred_score=True,
    )
    return ranked, activation_summary, llm_review, outer_summary


def apply_candidate_llm_overlay(
    df: pd.DataFrame,
    llm_review: Dict[str, Any],
    candidate_llm_cfg: Dict[str, Any],
) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    review = dict(llm_review.get("review", {}) or {})
    favored_symbols = {str(item).strip().upper() for item in list(review.get("favored_symbols", []) or []) if str(item).strip()}
    blocked_symbols = {str(item).strip().upper() for item in list(review.get("blocked_symbols", []) or []) if str(item).strip()}
    favored_mechanisms = {str(item).strip() for item in list(review.get("favored_mechanisms", []) or []) if str(item).strip()}
    favored_event_types = {str(item).strip() for item in list(review.get("favored_event_types", []) or []) if str(item).strip()}
    symbol_boost = float(candidate_llm_cfg.get("llm_symbol_boost", 0.10) or 0.10)
    mechanism_boost = float(candidate_llm_cfg.get("llm_mechanism_boost", 0.05) or 0.05)
    event_boost = float(candidate_llm_cfg.get("llm_event_boost", 0.04) or 0.04)
    blocked_penalty = float(candidate_llm_cfg.get("llm_blocked_penalty", 0.25) or 0.25)
    out["llm_symbol_favored"] = out["ts_code"].map(lambda x: str(x).strip().upper() in favored_symbols)
    out["llm_symbol_blocked"] = out["ts_code"].map(lambda x: str(x).strip().upper() in blocked_symbols)
    out["llm_mechanism_favored"] = out.get("primary_mechanism_group", pd.Series([""] * len(out.index), index=out.index)).map(lambda x: str(x).strip() in favored_mechanisms)
    out["llm_event_favored"] = out.get("primary_event_type", pd.Series([""] * len(out.index), index=out.index)).map(lambda x: str(x).strip() in favored_event_types)
    out["selection_score"] = pd.to_numeric(out.get("selection_score"), errors="coerce").fillna(0.0)
    out.loc[out["llm_symbol_favored"], "selection_score"] += symbol_boost
    out.loc[out["llm_mechanism_favored"], "selection_score"] += mechanism_boost
    out.loc[out["llm_event_favored"], "selection_score"] += event_boost
    out.loc[out["llm_symbol_blocked"], "selection_score"] = (out.loc[out["llm_symbol_blocked"], "selection_score"] - blocked_penalty).clip(lower=0.0)
    return out


def choose_candidate_pool(
    *,
    current_df: pd.DataFrame,
    candidate_source: str,
    broad_pool_df: pd.DataFrame,
    broad_candidate_limit: int,
    max_names: int,
    rec_cfg: Dict[str, Any],
    thesis_summary: Dict[str, Any],
    llm_review: Dict[str, Any],
    broad_execution_filter: Dict[str, Any],
) -> Tuple[pd.DataFrame, str, Dict[str, Any]]:
    weak_pool = int(thesis_summary.get("n_accepted", 0) or 0) <= int(rec_cfg.get("llm_candidate_weak_accept_threshold", 1) or 1)
    force_outer_pool = bool(rec_cfg.get("enable_intelligent_outer_allocator", True))
    if broad_pool_df.empty or not (candidate_source != "latest_portfolio_v1" or weak_pool or force_outer_pool):
        return current_df, candidate_source, broad_execution_filter
    preferred_count = int((llm_review.get("review", {}) or {}).get("target_candidate_count", broad_candidate_limit) or broad_candidate_limit)
    selected = sort_candidate_pool(
        broad_pool_df,
        include_outer_priority=True,
        include_activation_priority=True,
        include_pred_score=True,
    ).head(max(preferred_count, max_names * 3)).copy()
    if bool(rec_cfg.get("enable_intelligent_outer_allocator", True)):
        source = "outer_intelligence_pool"
    else:
        source = "broad_candidate_pool_llm" if bool(llm_review.get("enabled", False)) else "broad_candidate_pool"
    return selected, source, broad_execution_filter


def summarize_candidate_pool(
    *,
    broad_pool_df: pd.DataFrame,
    broad_candidate_limit: int,
    llm_review: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "broad_pool_count": int(len(broad_pool_df.index)),
        "broad_pool_limit": int(broad_candidate_limit),
        "tier_counts": {
            str(key): int(value)
            for key, value in broad_pool_df.get("candidate_tier", pd.Series(dtype="object")).value_counts().to_dict().items()
        },
        "alpha_family_counts": {
            str(key): int(value)
            for key, value in broad_pool_df.get("activation_alpha_family", pd.Series(dtype="object")).value_counts().to_dict().items()
        },
        "alpha_registry": summarize_alpha_registry(broad_pool_df),
        "llm_target_candidate_count": int((llm_review.get("review", {}) or {}).get("target_candidate_count", 0) or 0),
    }
