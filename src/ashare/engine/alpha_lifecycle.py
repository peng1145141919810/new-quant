from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pandas as pd


def _frame(df: pd.DataFrame | None) -> pd.DataFrame:
    return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _family_series(df: pd.DataFrame) -> pd.Series:
    if "activation_alpha_family" in df.columns:
        return df["activation_alpha_family"].fillna("unknown").astype(str)
    if "alpha_family" in df.columns:
        return df["alpha_family"].fillna("unknown").astype(str)
    return pd.Series(["unknown"] * len(df.index), index=df.index, dtype="object")


def _num(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce").fillna(0.0)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _promote_state(
    *,
    sample_count: int,
    exposure: float,
    pnl_proxy: float,
    pnl_yield: float,
    priority_mean: float,
    concentration: float,
) -> str:
    if sample_count <= 0 and exposure <= 0:
        return "inactive"
    if pnl_yield <= -0.03 and exposure >= 0.04:
        return "inactive"
    if pnl_proxy > 0 and pnl_yield >= 0 and priority_mean >= 0.52 and concentration <= 0.34:
        return "promote"
    if pnl_yield > -0.01 and priority_mean >= 0.40 and sample_count >= 2 and concentration <= 0.42:
        return "live"
    if (pnl_proxy < 0 or pnl_yield < -0.01) and exposure > 0.05:
        return "demote"
    return "shadow"


def build_alpha_lifecycle(
    *,
    candidate_df: pd.DataFrame | None,
    target_df: pd.DataFrame | None,
    position_df: pd.DataFrame | None,
    alpha_registry: Dict[str, Any] | None = None,
    alpha_attribution: Dict[str, Any] | None = None,
    market_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    candidate = _frame(candidate_df)
    target = _frame(target_df)
    position = _frame(position_df)
    alpha_registry = dict(alpha_registry or {})
    alpha_attribution = dict(alpha_attribution or {})
    market_state = dict(market_state or {})

    frames: List[pd.DataFrame] = []
    if not candidate.empty:
        candidate["_alpha_family"] = _family_series(candidate)
        candidate["_priority"] = _num(candidate, "alpha_activation_priority")
        candidate["_activation"] = _num(candidate, "data_activation_score")
        frames.append(candidate[["_alpha_family", "_priority", "_activation"]].copy())
    if not target.empty:
        target["_alpha_family"] = _family_series(target)
        target["_weight"] = _num(target, "portfolio_weight")
    if not position.empty:
        position["_alpha_family"] = _family_series(position)
        position["_market_value"] = _num(position, "market_value")
        position["_pnl_proxy"] = _num(position, "unrealized_pnl") + _num(position, "realized_pnl")

    family_keys: List[str] = []
    for source in [
        list((alpha_registry.get("family_counts", {}) or {}).keys()),
        list((alpha_attribution.get("target_weight_by_alpha_family", {}) or {}).keys()),
        list((alpha_attribution.get("pnl_proxy_by_alpha_family", {}) or {}).keys()),
        list(_family_series(candidate).dropna().astype(str).unique()) if not candidate.empty else [],
    ]:
        for item in source:
            key = str(item or "").strip()
            if key and key not in family_keys:
                family_keys.append(key)

    items: List[Dict[str, Any]] = []
    for family in family_keys:
        candidate_slice = candidate.loc[candidate["_alpha_family"].eq(family)].copy() if not candidate.empty else pd.DataFrame()
        target_slice = target.loc[target["_alpha_family"].eq(family)].copy() if not target.empty else pd.DataFrame()
        position_slice = position.loc[position["_alpha_family"].eq(family)].copy() if not position.empty else pd.DataFrame()
        sample_count = int(len(candidate_slice.index))
        exposure = float(target_slice["_weight"].sum()) if not target_slice.empty else _safe_float((alpha_attribution.get("target_weight_by_alpha_family", {}) or {}).get(family))
        pnl_proxy = float(position_slice["_pnl_proxy"].sum()) if not position_slice.empty else _safe_float((alpha_attribution.get("pnl_proxy_by_alpha_family", {}) or {}).get(family))
        market_value = float(position_slice["_market_value"].sum()) if not position_slice.empty else _safe_float((alpha_attribution.get("market_value_by_alpha_family", {}) or {}).get(family))
        pnl_yield = _safe_float((alpha_attribution.get("pnl_yield_by_alpha_family", {}) or {}).get(family))
        priority_mean = float(candidate_slice["_priority"].mean()) if not candidate_slice.empty else 0.0
        activation_mean = float(candidate_slice["_activation"].mean()) if not candidate_slice.empty else 0.0
        concentration = exposure if exposure > 0 else (market_value / max(sum((alpha_attribution.get("market_value_by_alpha_family", {}) or {}).values()) or [1.0], 1.0) if market_value > 0 else 0.0)
        state = _promote_state(
            sample_count=sample_count,
            exposure=exposure,
            pnl_proxy=pnl_proxy,
            pnl_yield=pnl_yield,
            priority_mean=priority_mean,
            concentration=concentration,
        )
        issues: List[str] = []
        if sample_count <= 1:
            issues.append("thin_sample")
        if pnl_proxy < 0:
            issues.append("negative_pnl_proxy")
        if pnl_yield < -0.01:
            issues.append("negative_pnl_yield")
        if concentration > 0.28:
            issues.append("crowded_exposure")
        if exposure <= 0 and sample_count > 0:
            issues.append("research_only")
        if priority_mean < 0.25 and sample_count > 0:
            issues.append("weak_priority")
        items.append(
            {
                "family": family,
                "sample_count": sample_count,
                "target_weight": round(exposure, 6),
                "market_value": round(market_value, 2),
                "pnl_proxy": round(pnl_proxy, 4),
                "pnl_yield": round(pnl_yield, 6),
                "priority_mean": round(priority_mean, 6),
                "activation_mean": round(activation_mean, 6),
                "concentration_proxy": round(concentration, 6),
                "state": state,
                "issues": issues,
            }
        )

    items = sorted(
        items,
        key=lambda row: (
            {"promote": 0, "live": 1, "shadow": 2, "demote": 3, "inactive": 4}.get(str(row.get("state")), 9),
            -float(row.get("priority_mean", 0.0)),
            -float(row.get("target_weight", 0.0)),
        ),
    )
    state_counts: Dict[str, int] = {}
    for item in items:
        state_counts[str(item["state"])] = int(state_counts.get(str(item["state"]), 0) + 1)
    return {
        "available": bool(items),
        "market_regime": str(market_state.get("market_regime", "") or ""),
        "items": items,
        "state_counts": state_counts,
        "promote_families": [str(item["family"]) for item in items if str(item.get("state")) == "promote"],
        "demote_families": [str(item["family"]) for item in items if str(item.get("state")) == "demote"],
        "shadow_families": [str(item["family"]) for item in items if str(item.get("state")) == "shadow"],
    }


def summarize_alpha_lifecycle_lines(lifecycle: Dict[str, Any] | None) -> List[str]:
    lifecycle = dict(lifecycle or {})
    items = list(lifecycle.get("items", []) or [])
    if not items:
        return ["No alpha lifecycle evidence available."]
    lines: List[str] = []
    for item in items[:6]:
        family = str(item.get("family", "unknown"))
        state = str(item.get("state", "unknown"))
        weight = _safe_float(item.get("target_weight"))
        pnl_proxy = _safe_float(item.get("pnl_proxy"))
        lines.append(f"{family}: {state}, weight={weight:.2%}, pnl_proxy={pnl_proxy:.2f}")
    return lines


def apply_alpha_lifecycle_weight_bias(
    *,
    df: pd.DataFrame,
    lifecycle: Dict[str, Any] | None,
    weight_column: str = "portfolio_weight",
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = _frame(df)
    if out.empty or weight_column not in out.columns:
        return out, {"applied": False, "reason": "missing_weight_column"}
    lifecycle = dict(lifecycle or {})
    items = list(lifecycle.get("items", []) or [])
    if not items:
        return out, {"applied": False, "reason": "empty_lifecycle"}
    family_col = _family_series(out)
    state_map = {str(item.get("family")): str(item.get("state")) for item in items if str(item.get("family") or "").strip()}
    multiplier_map = {
        "promote": 1.18,
        "live": 1.04,
        "shadow": 0.88,
        "demote": 0.68,
        "inactive": 0.0,
    }
    out["_alpha_family"] = family_col
    out["_alpha_state"] = out["_alpha_family"].map(lambda x: state_map.get(str(x), "shadow"))
    out["_alpha_multiplier"] = out["_alpha_state"].map(lambda x: float(multiplier_map.get(str(x), 0.88)))
    base = pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).clip(lower=0.0)
    out[weight_column] = base * out["_alpha_multiplier"]
    summary = {
        "applied": True,
        "weight_column": weight_column,
        "state_counts": {str(k): int(v) for k, v in out["_alpha_state"].value_counts().to_dict().items()},
        "family_multipliers": {
            str(item.get("family")): float(multiplier_map.get(str(item.get("state")), 0.88))
            for item in items
            if str(item.get("family") or "").strip()
        },
        "before_total_weight": round(float(base.sum()), 6),
        "after_total_weight": round(float(pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).sum()), 6),
    }
    out = out.drop(columns=["_alpha_family", "_alpha_state", "_alpha_multiplier"], errors="ignore")
    return out, summary
