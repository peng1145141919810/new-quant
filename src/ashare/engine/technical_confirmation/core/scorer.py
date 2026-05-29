from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _scaled(value: float, positive_scale: float) -> float:
    if abs(float(positive_scale)) <= 1e-9:
        return 0.0
    return _clip(float(value) / float(positive_scale), -1.0, 1.0)


def _bool_score(flag: bool) -> float:
    return 1.0 if bool(flag) else 0.0


def score_technical_frame(feature_df: pd.DataFrame, strictness: float, config_payload: Dict[str, Any]) -> pd.DataFrame:
    if feature_df.empty:
        return feature_df.copy()
    payload = feature_df.copy()
    entry_cfg = dict(config_payload.get("entry_thresholds", {}) or {})
    pilot_cfg = dict(config_payload.get("pilot_entry_thresholds", {}) or {})
    hold_cfg = dict(config_payload.get("existing_position_policy", {}) or {})
    payload["tech_trend_score"] = (
        payload.apply(lambda r: _bool_score(float(r.get("close", 0.0) or 0.0) > float(r.get("ma20", 0.0) or 0.0)), axis=1) * 0.35
        + payload.apply(lambda r: _bool_score(float(r.get("ma20", 0.0) or 0.0) > float(r.get("ma60", 0.0) or 0.0)), axis=1) * 0.25
        + payload["ret_20"].fillna(0.0).apply(lambda x: _clip(0.5 + _scaled(float(x), 0.15) * 0.5)) * 0.25
        + payload["ret_5"].fillna(0.0).apply(lambda x: _clip(0.5 + _scaled(float(x), 0.08) * 0.5)) * 0.15
    ).round(4)
    payload["tech_volume_score"] = (
        payload["amount_ratio_20"].fillna(1.0).apply(lambda x: _clip(0.5 + _scaled(float(x) - 1.0, 0.8) * 0.5)) * 0.6
        + payload["turnover_ratio_20"].fillna(1.0).apply(lambda x: _clip(0.5 + _scaled(float(x) - 1.0, 0.6) * 0.5)) * 0.4
    ).round(4)
    payload["tech_stretch_penalty"] = (
        payload["price_vs_ma20"].fillna(0.0).apply(lambda x: _clip(max(float(x) - 0.08, 0.0) / 0.10))
        * 0.55
        + payload["ret_3"].fillna(0.0).apply(lambda x: _clip(max(float(x) - 0.09, 0.0) / 0.10))
        * 0.30
        + payload["volatility_10"].fillna(0.0).apply(lambda x: _clip(max(float(x) - 0.04, 0.0) / 0.06))
        * 0.15
    ).round(4)
    payload["tech_hold_health"] = (
        payload.apply(lambda r: _bool_score(float(r.get("close", 0.0) or 0.0) > float(r.get("ma10", 0.0) or 0.0)), axis=1) * 0.35
        + payload.apply(lambda r: _bool_score(float(r.get("close", 0.0) or 0.0) > float(r.get("ma20", 0.0) or 0.0)), axis=1) * 0.35
        + payload["ret_5"].fillna(0.0).apply(lambda x: _clip(0.5 + _scaled(float(x), 0.10) * 0.5)) * 0.20
        + payload["volatility_10"].fillna(0.0).apply(lambda x: _clip(1.0 - max(float(x) - 0.03, 0.0) / 0.07)) * 0.10
    ).round(4)
    payload["tech_final_score"] = (
        payload["tech_trend_score"] * 0.5
        + payload["tech_volume_score"] * 0.2
        + payload["tech_hold_health"] * 0.2
        - payload["tech_stretch_penalty"] * 0.25
    ).round(4)
    strictness = float(strictness or 0.5)
    min_entry_score = (
        float(entry_cfg.get("base_min_score", 0.42) or 0.42)
        + strictness * float(entry_cfg.get("strictness_multiplier", 0.18) or 0.18)
    )
    min_trend_score = float(entry_cfg.get("min_trend_score", 0.38) or 0.38)
    max_stretch_penalty = float(entry_cfg.get("max_stretch_penalty", 0.58) or 0.58)
    min_hold_health = float(entry_cfg.get("min_hold_health", 0.32) or 0.32)
    pilot_enabled = bool(pilot_cfg.get("enabled", True))
    pilot_entry_score = max(0.0, min_entry_score - float(pilot_cfg.get("score_buffer", 0.10) or 0.10))
    pilot_trend_score = max(0.0, min_trend_score - float(pilot_cfg.get("trend_buffer", 0.08) or 0.08))
    pilot_max_stretch_penalty = float(pilot_cfg.get("max_stretch_penalty", max_stretch_penalty + 0.10) or (max_stretch_penalty + 0.10))
    watch_hold_health_floor = float(hold_cfg.get("watch_hold_health_floor", 0.12) or 0.12)
    payload["tech_allow_entry"] = (
        (payload["tech_final_score"] >= min_entry_score)
        & (payload["tech_trend_score"] >= min_trend_score)
        & (payload["tech_stretch_penalty"] <= max_stretch_penalty)
    )
    payload.loc[payload["is_existing_position"].astype(bool), "tech_allow_entry"] = (
        payload.loc[payload["is_existing_position"].astype(bool), "tech_hold_health"] >= min_hold_health
    )

    reasons = []
    entry_styles = []
    multipliers = []
    for _, row in payload.iterrows():
        is_hold = bool(row.get("is_existing_position", False))
        final_score = float(row.get("tech_final_score", 0.0) or 0.0)
        stretch = float(row.get("tech_stretch_penalty", 0.0) or 0.0)
        trend = float(row.get("tech_trend_score", 0.0) or 0.0)
        volume = float(row.get("tech_volume_score", 0.0) or 0.0)
        hold_health = float(row.get("tech_hold_health", 0.0) or 0.0)
        allow = bool(row.get("tech_allow_entry", False))
        pilot = False
        if (not allow) and (not is_hold) and pilot_enabled:
            if (
                final_score >= pilot_entry_score
                and trend >= pilot_trend_score
                and stretch <= pilot_max_stretch_penalty
            ):
                allow = True
                pilot = True
        if not allow:
            if stretch > max_stretch_penalty:
                reason = "overheated"
            elif trend < min_trend_score:
                reason = "trend_not_confirmed"
            elif is_hold and hold_health < min_hold_health:
                reason = "hold_health_weak"
            else:
                reason = "volume_not_confirmed"
        elif pilot:
            reason = "pilot_entry"
        elif stretch >= 0.30:
            reason = "allow_but_wait_pullback"
        else:
            reason = "confirmed"
        if not allow:
            entry_style = "no_entry" if not is_hold else "reduce_watch"
        elif pilot:
            entry_style = "pilot"
        elif stretch >= 0.30:
            entry_style = "wait"
        elif volume >= 0.60 and trend >= 0.60:
            entry_style = "breakout"
        else:
            entry_style = "pullback"
        if not allow and not is_hold:
            multiplier = 0.0
        elif not allow and is_hold:
            if hold_health >= watch_hold_health_floor:
                multiplier = max(
                    float(hold_cfg.get("reduce_multiplier_floor", 0.60) or 0.60),
                    min(
                        float(hold_cfg.get("reduce_multiplier_cap", 0.82) or 0.82),
                        0.58 + final_score * 0.35 + hold_health * 0.25,
                    ),
                )
            else:
                multiplier = 0.45
        elif pilot:
            multiplier = max(
                float(pilot_cfg.get("weight_floor", 0.28) or 0.28),
                min(
                    float(pilot_cfg.get("weight_cap", 0.52) or 0.52),
                    0.22 + final_score * 0.50 + volume * 0.12,
                ),
            )
        elif stretch >= 0.30:
            multiplier = max(0.62, min(0.92, 0.64 + final_score * 0.30 - stretch * 0.08))
        else:
            multiplier = max(0.55, min(1.15, 0.75 + final_score * 0.55 - stretch * 0.20))
        reasons.append(reason)
        entry_styles.append(entry_style)
        multipliers.append(round(multiplier, 4))
    payload["tech_gate_reason"] = reasons
    payload["tech_entry_style"] = entry_styles
    payload["tech_weight_multiplier"] = multipliers
    return payload
