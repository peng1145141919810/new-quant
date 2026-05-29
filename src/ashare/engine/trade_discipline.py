from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd


def _frame(df: pd.DataFrame | None) -> pd.DataFrame:
    return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _family_series(df: pd.DataFrame) -> pd.Series:
    if "activation_alpha_family" in df.columns:
        return df["activation_alpha_family"].fillna("unknown").astype(str)
    if "alpha_family" in df.columns:
        return df["alpha_family"].fillna("unknown").astype(str)
    return pd.Series(["unknown"] * len(df.index), index=df.index, dtype="object")


def _symbol_series(df: pd.DataFrame) -> pd.Series:
    for col in ("ts_code", "symbol", "code"):
        if col in df.columns:
            return df[col].fillna("").astype(str).str.upper()
    return pd.Series([""] * len(df.index), index=df.index, dtype="object")


def _weight_series(df: pd.DataFrame, column: str = "portfolio_weight") -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df.index), index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce").fillna(0.0).clip(lower=0.0)


def _target_slots(nav: float) -> int:
    if nav <= 0:
        return 6
    if nav < 50000:
        return 5
    if nav < 150000:
        return 8
    if nav < 500000:
        return 12
    if nav < 1500000:
        return 16
    return 22


def build_trade_discipline(
    *,
    target_df: pd.DataFrame | None,
    position_df: pd.DataFrame | None,
    market_state: Dict[str, Any] | None,
    account_ctx: Dict[str, Any] | None,
    alpha_lifecycle: Dict[str, Any] | None,
    operating_brain: Dict[str, Any] | None,
) -> Dict[str, Any]:
    target = _frame(target_df)
    position = _frame(position_df)
    market_state = dict(market_state or {})
    account_ctx = dict(account_ctx or {})
    alpha_lifecycle = dict(alpha_lifecycle or {})
    operating_brain = dict((operating_brain or {}).get("review", {}) or {})

    nav = _safe_float(account_ctx.get("nav"))
    cash = _safe_float(account_ctx.get("cash"))
    cash_ratio = cash / max(nav, 1e-9) if nav > 0 else 0.0
    positions_count = int(account_ctx.get("positions_count", 0) or 0)
    risk_budget = _safe_float(market_state.get("risk_budget_multiplier"), 1.0)
    policy = str(market_state.get("new_position_policy", "allow") or "allow").strip().lower()
    market_regime = str(market_state.get("market_regime", "") or "").strip().lower()
    dispatch_brain = dict(operating_brain.get("dispatch_brain", {}) or {})
    preferred_posture = str(dispatch_brain.get("preferred_posture", "balanced") or "balanced").strip().lower()
    cash_posture = str(dispatch_brain.get("cash_posture", "hold_buffer") or "hold_buffer").strip().lower()

    out = target.copy()
    if not out.empty:
        out["_alpha_family"] = _family_series(out)
        out["_symbol"] = _symbol_series(out)
        out["_weight"] = _weight_series(out)
    pos = position.copy()
    if not pos.empty:
        pos["_alpha_family"] = _family_series(pos)
        pos["_symbol"] = _symbol_series(pos)
        pos["_market_value"] = pd.to_numeric(pos.get("market_value"), errors="coerce").fillna(0.0)
        pos["_pnl_proxy"] = pd.to_numeric(pos.get("unrealized_pnl"), errors="coerce").fillna(0.0) + pd.to_numeric(pos.get("realized_pnl"), errors="coerce").fillna(0.0)

    target_slots = _target_slots(nav)
    top_weights = out["_weight"].sort_values(ascending=False).tolist() if not out.empty else []
    top1_weight = float(top_weights[0]) if top_weights else 0.0
    total_weight = float(out["_weight"].sum()) if not out.empty else 0.0
    hhi = float(sum(w * w for w in top_weights))
    concentration_risk = "ok"
    if top1_weight >= 0.20 or hhi >= 0.18:
        concentration_risk = "high"
    elif top1_weight >= 0.14 or hhi >= 0.12:
        concentration_risk = "elevated"

    lifecycle_items = list(alpha_lifecycle.get("items", []) or [])
    state_map = {str(item.get("family")): str(item.get("state")) for item in lifecycle_items if str(item.get("family") or "").strip()}
    demote_families = [family for family, state in state_map.items() if state in {"demote", "inactive"}]
    promote_families = [family for family, state in state_map.items() if state in {"promote", "live"}]

    posture = preferred_posture if preferred_posture in {"aggressive", "balanced", "defensive"} else "balanced"
    if policy in {"reduce_only", "no_new_positions"}:
        posture = "reduce_only"
    elif market_regime in {"panic"}:
        posture = "reduce_only"
    elif market_regime in {"caution"} or concentration_risk == "high" or cash_ratio < 0.06:
        posture = "defensive"
    elif posture == "aggressive" and (cash_ratio < 0.10 or concentration_risk != "ok"):
        posture = "balanced"

    new_position_budget = 1.0
    add_multiplier = 1.0
    sell_pressure = 0.15
    if posture == "reduce_only":
        new_position_budget = 0.0
        add_multiplier = 0.0
        sell_pressure = 0.95
    elif posture == "defensive":
        new_position_budget = 0.35 if policy == "allow" else 0.20
        add_multiplier = 0.72 * min(max(risk_budget, 0.35), 1.0)
        sell_pressure = 0.60
    elif posture == "balanced":
        new_position_budget = 0.70
        add_multiplier = 0.92 * min(max(risk_budget, 0.45), 1.15)
        sell_pressure = 0.30
    else:
        new_position_budget = 0.95
        add_multiplier = min(max(risk_budget, 0.55), 1.25)
        sell_pressure = 0.20

    if cash_posture == "raise_cash":
        new_position_budget *= 0.55
        add_multiplier *= 0.72
        sell_pressure = max(sell_pressure, 0.72)
    elif cash_posture == "hold_buffer":
        new_position_budget *= 0.85
        add_multiplier *= 0.90

    family_bias: Dict[str, float] = {}
    for family, state in state_map.items():
        multiplier = 1.0
        if state == "promote":
            multiplier *= 1.12 if posture not in {"reduce_only", "defensive"} else 1.02
        elif state == "live":
            multiplier *= 1.04
        elif state == "shadow":
            multiplier *= 0.88
        elif state == "demote":
            multiplier *= 0.62
        elif state == "inactive":
            multiplier *= 0.40
        if posture == "reduce_only":
            multiplier *= 0.0 if family not in promote_families else 0.35
        elif posture == "defensive" and family in demote_families:
            multiplier *= 0.75
        family_bias[family] = round(float(multiplier), 4)

    sell_priority_rows: List[Dict[str, Any]] = []
    if not pos.empty:
        pos["_family_state"] = pos["_alpha_family"].map(lambda value: state_map.get(str(value), "shadow"))
        pos["_sell_priority"] = 0.0
        pos.loc[pos["_family_state"].isin(["demote", "inactive"]), "_sell_priority"] += 0.50
        pos.loc[pos["_pnl_proxy"].lt(0), "_sell_priority"] += 0.20
        pos.loc[pos["_market_value"].gt(max(nav, 1.0) * 0.10), "_sell_priority"] += 0.20
        for _, row in pos.sort_values(["_sell_priority", "_market_value"], ascending=[False, False]).head(12).iterrows():
            if float(row.get("_sell_priority", 0.0) or 0.0) <= 0:
                continue
            sell_priority_rows.append(
                {
                    "symbol": str(row.get("_symbol", "") or ""),
                    "alpha_family": str(row.get("_alpha_family", "") or ""),
                    "family_state": str(row.get("_family_state", "") or ""),
                    "sell_priority": round(_safe_float(row.get("_sell_priority")), 4),
                    "market_value": round(_safe_float(row.get("_market_value")), 2),
                    "pnl_proxy": round(_safe_float(row.get("_pnl_proxy")), 4),
                }
            )

    return {
        "available": True,
        "posture": posture,
        "cash_posture": cash_posture,
        "market_regime": market_regime,
        "new_position_budget": round(float(new_position_budget), 4),
        "add_multiplier": round(float(add_multiplier), 4),
        "sell_pressure": round(float(sell_pressure), 4),
        "target_slots": int(target_slots),
        "positions_count": int(positions_count),
        "cash_ratio": round(float(cash_ratio), 4),
        "target_total_weight": round(float(total_weight), 6),
        "top1_weight": round(float(top1_weight), 6),
        "hhi": round(float(hhi), 6),
        "concentration_risk": concentration_risk,
        "promote_families": promote_families,
        "demote_families": demote_families,
        "family_bias": family_bias,
        "sell_priority_symbols": sell_priority_rows,
    }


def apply_trade_discipline_weights(
    *,
    df: pd.DataFrame,
    discipline: Dict[str, Any] | None,
    weight_column: str = "portfolio_weight",
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = _frame(df)
    if out.empty or weight_column not in out.columns:
        return out, {"applied": False, "reason": "missing_weight_column"}
    discipline = dict(discipline or {})
    if not discipline:
        return out, {"applied": False, "reason": "empty_discipline"}

    base = _weight_series(out, weight_column)
    family_col = _family_series(out)
    bias_map = {str(k): _safe_float(v, 1.0) for k, v in dict(discipline.get("family_bias", {}) or {}).items()}
    posture = str(discipline.get("posture", "balanced") or "balanced").lower()
    add_multiplier = _safe_float(discipline.get("add_multiplier"), 1.0)
    new_position_budget = _safe_float(discipline.get("new_position_budget"), 1.0)
    target_slots = max(int(discipline.get("target_slots", 1) or 1), 1)
    slot_cap = 1.0 / target_slots

    out["_alpha_family"] = family_col
    out["_discipline_bias"] = out["_alpha_family"].map(lambda value: bias_map.get(str(value), 0.88))
    out["_existing"] = pd.to_numeric(out.get("is_existing_position"), errors="coerce").fillna(0.0) if "is_existing_position" in out.columns else 0.0
    out[weight_column] = base * out["_discipline_bias"]
    if posture in {"reduce_only", "defensive"}:
        out.loc[out["_existing"].le(0), weight_column] = pd.to_numeric(out.loc[out["_existing"].le(0), weight_column], errors="coerce").fillna(0.0) * new_position_budget
    out.loc[pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).gt(slot_cap * 1.25), weight_column] = slot_cap * 1.25
    out[weight_column] = pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0) * add_multiplier

    summary = {
        "applied": True,
        "posture": posture,
        "new_position_budget": round(float(new_position_budget), 4),
        "add_multiplier": round(float(add_multiplier), 4),
        "target_slots": int(target_slots),
        "family_bias_count": int(len(bias_map)),
        "before_total_weight": round(float(base.sum()), 6),
        "after_total_weight": round(float(pd.to_numeric(out[weight_column], errors="coerce").fillna(0.0).sum()), 6),
    }
    out = out.drop(columns=["_alpha_family", "_discipline_bias", "_existing"], errors="ignore")
    return out, summary
