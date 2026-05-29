from __future__ import annotations

import math
from typing import Any, Dict, Tuple

import pandas as pd


def account_allocator_profile(
    *,
    account_ctx: Dict[str, Any],
    limits: Dict[str, float],
) -> Dict[str, Any]:
    nav = float(account_ctx.get("nav") or 0.0)
    cash = float(account_ctx.get("cash") or 0.0)
    positions_count = int(account_ctx.get("positions_count") or 0)
    cash_ratio = cash / max(nav, 1e-9) if nav > 0 else 0.0
    if nav <= 0:
        bucket = "unknown"
    elif nav < 50000:
        bucket = "micro"
    elif nav < 150000:
        bucket = "small"
    elif nav < 500000:
        bucket = "mid"
    elif nav < 1500000:
        bucket = "large"
    else:
        bucket = "institutional"
    target_slots = {"micro": 5, "small": 8, "mid": 12, "large": 16, "institutional": 22}.get(bucket, 6)
    desired_single_name_cap = min(float(limits.get("single_name_cap", 0.2) or 0.2), 1.0 / max(target_slots - 1, 1) * 1.35)
    desired_family_cap = min(0.55, max(desired_single_name_cap * 2.2, 0.16))
    desired_industry_cap = min(0.42, max(desired_single_name_cap * 1.85, 0.14))
    return {
        "bucket": bucket,
        "cash_ratio": round(cash_ratio, 4),
        "positions_count": positions_count,
        "target_slots": int(target_slots),
        "desired_single_name_cap": round(float(desired_single_name_cap), 6),
        "desired_family_cap": round(float(desired_family_cap), 6),
        "desired_industry_cap": round(float(desired_industry_cap), 6),
        "cash_posture": "deploy" if cash_ratio >= 0.18 else ("balanced" if cash_ratio >= 0.08 else "hold_buffer"),
        "avoid_concentration": True,
    }


def min_executable_trade_value(price: float, lot_size: int, min_trade_value: float) -> float:
    px = float(price or 0.0)
    if px <= 0:
        return 0.0
    share_unit = max(int(lot_size or 0), 1)
    required_shares = share_unit
    if float(min_trade_value or 0.0) > 0:
        required_shares = max(
            required_shares,
            int(math.ceil(float(min_trade_value) / px / share_unit) * share_unit),
        )
    return float(required_shares) * px


def account_size_adjusted_limits(
    *,
    limits: Dict[str, float],
    rec_cfg: Dict[str, Any],
    broker_cfg: Dict[str, Any],
    account_ctx: Dict[str, Any],
    candidate_df: pd.DataFrame,
) -> tuple[Dict[str, float], Dict[str, Any]]:
    adjusted = dict(limits)
    if not bool(rec_cfg.get("account_size_aware_sizing", True)):
        return adjusted, {"enabled": False}
    nav = float(account_ctx.get("nav") or 0.0)
    cash = float(account_ctx.get("cash") or 0.0)
    if nav <= 0 or cash <= 0:
        out = dict(account_ctx)
        out.update({"enabled": True, "applied": False, "reason": "missing_account_nav_or_cash"})
        return adjusted, out
    lot_size = max(int(broker_cfg.get("lot_size", 100) or 100), 1)
    min_trade_value = max(float(broker_cfg.get("min_trade_value", 2000.0) or 2000.0), 0.0)
    cash_reserve_ratio = max(float(broker_cfg.get("cash_reserve_ratio", 0.02) or 0.02), 0.0)
    slot_budget_ratio = min(max(float(rec_cfg.get("account_size_slot_budget_ratio", 0.96) or 0.96), 0.2), 1.0)
    min_weight_buffer = min(max(float(rec_cfg.get("account_size_min_weight_buffer", 1.05) or 1.05), 1.0), 2.0)
    max_single_name_cap = min(max(float(rec_cfg.get("account_size_max_single_name_cap", 0.35) or 0.35), 0.05), 1.0)
    spendable_cash = min(cash, nav * max(0.0, 1.0 - cash_reserve_ratio) * float(adjusted.get("total_exposure_cap", 1.0))) * slot_budget_ratio
    price_series = pd.to_numeric(candidate_df.get("price"), errors="coerce").fillna(0.0)
    slot_costs = sorted(
        min_executable_trade_value(price=float(price), lot_size=lot_size, min_trade_value=min_trade_value)
        for price in price_series.tolist()
        if float(price) > 0
    )
    out = dict(account_ctx)
    if not slot_costs or spendable_cash <= 0:
        out.update({"enabled": True, "applied": False, "reason": "missing_prices_or_spendable_cash"})
        return adjusted, out
    cumulative = 0.0
    affordable_names = 0
    for cost in slot_costs:
        if cumulative + cost > spendable_cash + 1e-9:
            break
        cumulative += cost
        affordable_names += 1
    affordable_names = max(1, affordable_names)
    adjusted["base_max_names_account"] = int(adjusted.get("max_names", 0))
    cap_feasible_names = int(affordable_names)
    candidate_max_names = max(1, min(int(adjusted.get("max_names", 1)), int(affordable_names)))
    required_slot_value = 0.0
    min_executable_weight = 0.0
    target_equal_weight = 0.0
    base_single_name_cap = float(adjusted.get("single_name_cap", 0.0))
    while candidate_max_names >= 1:
        required_slot_value = float(slot_costs[min(int(candidate_max_names) - 1, len(slot_costs) - 1)])
        min_executable_weight = required_slot_value / max(nav, 1e-9)
        target_equal_weight = float(adjusted.get("total_exposure_cap", 1.0)) / max(int(candidate_max_names), 1)
        candidate_single_name_cap = min(
            max_single_name_cap,
            max(base_single_name_cap, target_equal_weight * 1.15, min_executable_weight * min_weight_buffer),
        )
        cap_feasible_names = sum(
            1
            for cost in slot_costs
            if (float(cost) / max(nav, 1e-9) * min_weight_buffer) <= float(candidate_single_name_cap) + 1e-9
        )
        if cap_feasible_names >= candidate_max_names:
            adjusted["max_names"] = int(candidate_max_names)
            adjusted["single_name_cap"] = float(candidate_single_name_cap)
            break
        candidate_max_names -= 1
    else:
        adjusted["max_names"] = 1
        adjusted["single_name_cap"] = min(max_single_name_cap, max(base_single_name_cap, 1.0))
    out.update(
        {
            "enabled": True,
            "applied": True,
            "lot_size": lot_size,
            "min_trade_value": min_trade_value,
            "cash_reserve_ratio": cash_reserve_ratio,
            "slot_budget_ratio": slot_budget_ratio,
            "spendable_cash": round(spendable_cash, 4),
            "affordable_names": int(affordable_names),
            "cap_feasible_names": int(cap_feasible_names),
            "required_slot_value": round(required_slot_value, 4),
            "min_executable_weight": round(min_executable_weight, 6),
            "adjusted_max_names": int(adjusted["max_names"]),
            "adjusted_single_name_cap": round(float(adjusted["single_name_cap"]), 6),
        }
    )
    return adjusted, out


def diversify_portfolio_weights(
    *,
    df: pd.DataFrame,
    account_profile: Dict[str, Any],
    total_exposure_cap: float,
    single_name_cap: float,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    if out.empty or "portfolio_weight" not in out.columns:
        return out, {"applied": False, "reason": "missing_portfolio_weight"}
    desired_cap = min(float(account_profile.get("desired_single_name_cap", single_name_cap) or single_name_cap), float(single_name_cap))
    weights = pd.to_numeric(out["portfolio_weight"], errors="coerce").fillna(0.0).clip(lower=0.0, upper=desired_cap)
    before_hhi = float((weights ** 2).sum())
    family_summary_before: Dict[str, float] = {}
    industry_summary_before: Dict[str, float] = {}
    family_cap = float(account_profile.get("desired_family_cap", max(desired_cap * 2.2, 0.16)) or max(desired_cap * 2.2, 0.16))
    industry_cap = float(account_profile.get("desired_industry_cap", max(desired_cap * 1.85, 0.14)) or max(desired_cap * 1.85, 0.14))

    if "activation_alpha_family" in out.columns:
        family_series = out["activation_alpha_family"].fillna("unknown").astype(str)
        family_summary_before = {
            str(k): round(float(v), 6)
            for k, v in pd.DataFrame({"family": family_series, "weight": weights}).groupby("family")["weight"].sum().to_dict().items()
        }
        for family, total in sorted(family_summary_before.items(), key=lambda item: item[1], reverse=True):
            if total <= family_cap + 1e-9:
                continue
            overflow = total - family_cap
            idx = family_series.eq(family)
            local_weights = weights.loc[idx]
            if float(local_weights.sum()) <= 0:
                continue
            weights.loc[idx] = (local_weights * (family_cap / float(local_weights.sum()))).clip(lower=0.0, upper=desired_cap)
            remainder_idx = ~idx
            if bool(remainder_idx.any()):
                room = (desired_cap - weights.loc[remainder_idx]).clip(lower=0.0)
                room_sum = float(room.sum())
                if room_sum > 1e-9:
                    weights.loc[remainder_idx] = (weights.loc[remainder_idx] + room / room_sum * overflow).clip(upper=desired_cap)

    if "industry" in out.columns:
        industry_series = out["industry"].fillna("unknown").astype(str)
        industry_summary_before = {
            str(k): round(float(v), 6)
            for k, v in pd.DataFrame({"industry": industry_series, "weight": weights}).groupby("industry")["weight"].sum().to_dict().items()
        }
        for industry, total in sorted(industry_summary_before.items(), key=lambda item: item[1], reverse=True):
            if total <= industry_cap + 1e-9:
                continue
            overflow = total - industry_cap
            idx = industry_series.eq(industry)
            local_weights = weights.loc[idx]
            if float(local_weights.sum()) <= 0:
                continue
            weights.loc[idx] = (local_weights * (industry_cap / float(local_weights.sum()))).clip(lower=0.0, upper=desired_cap)
            remainder_idx = ~idx
            if bool(remainder_idx.any()):
                room = (desired_cap - weights.loc[remainder_idx]).clip(lower=0.0)
                room_sum = float(room.sum())
                if room_sum > 1e-9:
                    weights.loc[remainder_idx] = (weights.loc[remainder_idx] + room / room_sum * overflow).clip(upper=desired_cap)

    total = float(weights.sum())
    if total > float(total_exposure_cap) and total > 0:
        weights = weights * (float(total_exposure_cap) / total)
    remaining = max(float(total_exposure_cap) - float(weights.sum()), 0.0)
    for _ in range(8):
        if remaining <= 1e-6:
            break
        eligible = weights < desired_cap - 1e-9
        if not bool(eligible.any()):
            break
        add = remaining / max(int(eligible.sum()), 1)
        weights.loc[eligible] = (weights.loc[eligible] + add).clip(upper=desired_cap)
        remaining = max(float(total_exposure_cap) - float(weights.sum()), 0.0)
    out["portfolio_weight"] = weights
    after_hhi = float((weights ** 2).sum())
    family_summary_after: Dict[str, float] = {}
    industry_summary_after: Dict[str, float] = {}
    if "activation_alpha_family" in out.columns:
        family_summary_after = {
            str(k): round(float(v), 6)
            for k, v in pd.DataFrame({"family": out["activation_alpha_family"].fillna("unknown").astype(str), "weight": weights}).groupby("family")["weight"].sum().to_dict().items()
        }
    if "industry" in out.columns:
        industry_summary_after = {
            str(k): round(float(v), 6)
            for k, v in pd.DataFrame({"industry": out["industry"].fillna("unknown").astype(str), "weight": weights}).groupby("industry")["weight"].sum().to_dict().items()
        }
    return out, {
        "applied": True,
        "bucket": str(account_profile.get("bucket", "") or ""),
        "target_slots": int(account_profile.get("target_slots", 0) or 0),
        "desired_single_name_cap": round(desired_cap, 6),
        "desired_family_cap": round(family_cap, 6),
        "desired_industry_cap": round(industry_cap, 6),
        "before_hhi": round(before_hhi, 6),
        "after_hhi": round(after_hhi, 6),
        "family_weight_before": family_summary_before,
        "family_weight_after": family_summary_after,
        "industry_weight_before": industry_summary_before,
        "industry_weight_after": industry_summary_after,
    }


def enforce_min_executable_weights(
    *,
    df: pd.DataFrame,
    total_exposure_cap: float,
    single_name_cap: float,
    nav: float,
    lot_size: int,
    min_trade_value: float,
    weight_buffer: float,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    if out.empty or "price" not in out.columns or "portfolio_weight" not in out.columns or nav <= 0:
        return out, {"applied": False, "reason": "missing_prerequisites"}
    out["price"] = pd.to_numeric(out["price"], errors="coerce").fillna(0.0)
    out["portfolio_weight"] = pd.to_numeric(out["portfolio_weight"], errors="coerce").fillna(0.0).clip(lower=0.0, upper=single_name_cap)
    out = out.loc[out["price"] > 0].copy()
    if out.empty:
        return out, {"applied": False, "reason": "missing_positive_prices"}
    if "selection_score" not in out.columns:
        out["selection_score"] = out["portfolio_weight"]
    while len(out.index) > 1:
        min_floor = out["price"].map(lambda p: min_executable_trade_value(float(p), lot_size, min_trade_value) / nav * weight_buffer)
        if float(min_floor.sum()) <= float(total_exposure_cap) + 1e-9:
            break
        weakest_idx = out.sort_values(["selection_score", "portfolio_weight"], ascending=[True, True]).index[0]
        out = out.drop(index=weakest_idx).reset_index(drop=True)
    while len(out.index) > 1:
        floor_weights = out["price"].map(lambda p: min_executable_trade_value(float(p), lot_size, min_trade_value) / nav * weight_buffer).clip(lower=0.0, upper=single_name_cap)
        candidate_weights = pd.concat([out["portfolio_weight"], floor_weights], axis=1).max(axis=1)
        if float(candidate_weights.sum()) <= float(total_exposure_cap) + 1e-9:
            break
        weakest_idx = out.sort_values(["selection_score", "portfolio_weight"], ascending=[True, True]).index[0]
        out = out.drop(index=weakest_idx).reset_index(drop=True)
    floor_weights = out["price"].map(lambda p: min_executable_trade_value(float(p), lot_size, min_trade_value) / nav * weight_buffer).clip(lower=0.0, upper=single_name_cap)
    out["portfolio_weight"] = pd.concat([out["portfolio_weight"], floor_weights], axis=1).max(axis=1)
    return out, {
        "applied": True,
        "selected_names": int(len(out.index)),
        "min_floor_weight": round(float(floor_weights.min()) if not floor_weights.empty else 0.0, 6),
        "max_floor_weight": round(float(floor_weights.max()) if not floor_weights.empty else 0.0, 6),
        "final_total_weight": round(float(out["portfolio_weight"].sum()), 6),
    }


def select_account_aware_candidates(
    *,
    df: pd.DataFrame,
    max_names: int,
    total_exposure_cap: float,
    nav: float,
    lot_size: int,
    min_trade_value: float,
    weight_buffer: float,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    if out.empty or "price" not in out.columns or nav <= 0:
        return out.head(max_names).copy(), {"applied": False, "reason": "missing_prerequisites"}
    out = out.reset_index(drop=True)
    out["price"] = pd.to_numeric(out["price"], errors="coerce").fillna(0.0)
    out = out.loc[out["price"] > 0].copy()
    if out.empty:
        return out.head(max_names).copy(), {"applied": False, "reason": "missing_positive_prices"}
    out["required_floor_weight"] = out["price"].map(lambda p: min_executable_trade_value(float(p), lot_size, min_trade_value) / nav * weight_buffer)
    out["selection_score"] = pd.to_numeric(out.get("portfolio_weight"), errors="coerce").fillna(0.0)
    if "integrated_thesis_score" in out.columns:
        out["selection_score"] += pd.to_numeric(out.get("integrated_thesis_score"), errors="coerce").fillna(0.0) * 0.05
    if "tech_final_score" in out.columns:
        out["selection_score"] += pd.to_numeric(out.get("tech_final_score"), errors="coerce").fillna(0.0) * 0.02
    out["selection_density"] = out["selection_score"] / out["required_floor_weight"].clip(lower=1e-9)
    ranked = out.sort_values(["selection_density", "selection_score", "required_floor_weight"], ascending=[False, False, True]).reset_index(drop=True)
    picked: list[int] = []
    floor_sum = 0.0
    for idx, row in ranked.iterrows():
        floor_weight = float(row.get("required_floor_weight") or 0.0)
        if floor_weight <= 0:
            continue
        if len(picked) >= max_names:
            break
        if floor_sum + floor_weight > float(total_exposure_cap) + 1e-9:
            continue
        picked.append(int(idx))
        floor_sum += floor_weight
    if not picked:
        picked = [0]
    selected = ranked.iloc[picked].copy().sort_values(["selection_score", "portfolio_weight"], ascending=[False, False]).reset_index(drop=True)
    return selected, {
        "applied": True,
        "candidate_rows": int(len(out.index)),
        "selected_names": int(len(selected.index)),
        "selected_floor_sum": round(float(floor_sum), 6),
        "max_names": int(max_names),
    }


def clean_target_position_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    rename_map = {
        "date_x": "date",
        "date_y": "tech_date",
        "close_x": "close",
        "close_y": "tech_close",
    }
    for old, new in rename_map.items():
        if old in out.columns and new not in out.columns:
            out = out.rename(columns={old: new})
    return out


def rebalance_to_target_fill(
    *,
    df: pd.DataFrame,
    target_total: float,
    single_name_cap: float,
) -> tuple[pd.DataFrame, float, float]:
    out = df.copy()
    if out.empty or "portfolio_weight" not in out.columns:
        return out, 0.0, 0.0
    cap_series = pd.Series(float(single_name_cap), index=out.index)
    if "target_weight_cap_v2a" in out.columns:
        cap_series = pd.to_numeric(out["target_weight_cap_v2a"], errors="coerce").fillna(float(single_name_cap)).clip(lower=0.0, upper=float(single_name_cap))
    weights = pd.to_numeric(out["portfolio_weight"], errors="coerce").fillna(0.0).clip(lower=0.0)
    weights = pd.concat([weights, cap_series], axis=1).min(axis=1)
    before_total = float(weights.sum())
    target_total = max(0.0, float(target_total))
    if before_total >= target_total - 1e-9:
        out["portfolio_weight"] = weights
        return out, before_total, float(weights.sum())
    if before_total <= 1e-9:
        if len(out.index) <= 0:
            out["portfolio_weight"] = weights
            return out, before_total, 0.0
        eligible_count = max(len(out.index), 1)
        base_equal = target_total / eligible_count
        weights[:] = pd.Series([min(float(cap_series.iloc[idx]), base_equal) for idx in range(len(out.index))], index=out.index)
    else:
        for _ in range(8):
            current_total = float(weights.sum())
            gap = target_total - current_total
            if gap <= 1e-6:
                break
            eligible = weights < (cap_series - 1e-9)
            if not bool(eligible.any()):
                break
            eligible_weights = weights.loc[eligible]
            eligible_caps = cap_series.loc[eligible]
            capacity = float((eligible_caps - eligible_weights).clip(lower=0.0).sum())
            if capacity <= 1e-9:
                break
            base = eligible_weights.copy()
            if float(base.sum()) <= 1e-9:
                add = pd.Series(gap / max(len(base.index), 1), index=base.index)
            else:
                add = base / float(base.sum()) * gap
            add = add.clip(upper=eligible_caps - eligible_weights)
            weights.loc[eligible] = pd.concat([(eligible_weights + add), eligible_caps], axis=1).min(axis=1)
    out["portfolio_weight"] = pd.concat([weights, cap_series], axis=1).min(axis=1)
    return out, before_total, float(out["portfolio_weight"].sum())
