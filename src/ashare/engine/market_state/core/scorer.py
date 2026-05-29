from __future__ import annotations

from typing import Any, Dict


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _scaled(value: float, scale: float) -> float:
    if abs(scale) <= 1e-9:
        return 0.0
    return _clip(float(value) / float(scale))


def compute_market_scores(feature_snapshot: Dict[str, Any], config_payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics = dict(feature_snapshot.get("metrics", {}) or {})
    weights = dict(config_payload.get("score_weights", {}) or {})
    trend_score = (
        _scaled(metrics.get("hs300_ret_5", 0.0), 0.04) * 0.35
        + _scaled(metrics.get("hs300_ret_20", 0.0), 0.08) * 0.30
        + _scaled(metrics.get("hs300_ma20_gap", 0.0), 0.06) * 0.20
        + _scaled(metrics.get("avg_pct_chg", 0.0), 0.02) * 0.15
    )
    breadth_score = (
        _scaled(metrics.get("advancers_ratio", 0.5) - 0.5, 0.25) * 0.45
        + _scaled(metrics.get("strong_up_ratio", 0.0) - metrics.get("large_drop_ratio", 0.0), 0.10) * 0.35
        + _scaled(0.03 - metrics.get("limit_down_ratio", 0.0), 0.03) * 0.20
    )
    liquidity_score = (
        _scaled(metrics.get("total_amount_ratio_20", 1.0) - 1.0, 0.35) * 0.45
        + _scaled(metrics.get("median_turnover_ratio_20", 1.0) - 1.0, 0.30) * 0.35
        + _scaled(metrics.get("active_amount_share", 0.0) - 0.2, 0.25) * 0.20
    )
    style_signal = _scaled(metrics.get("size_spread_1d", 0.0), 0.03)
    mech_scores = dict(feature_snapshot.get("mechanism_scores", {}) or {})
    lead = max(mech_scores.values()) if mech_scores else 0.0
    trail = min(mech_scores.values()) if mech_scores else 0.0
    style_score = style_signal * 0.65 + _scaled(lead - trail, 0.25) * 0.35
    market_regime_score = (
        trend_score * float(weights.get("trend", 0.35) or 0.35)
        + breadth_score * float(weights.get("breadth", 0.30) or 0.30)
        + liquidity_score * float(weights.get("liquidity", 0.20) or 0.20)
        + style_score * float(weights.get("style", 0.15) or 0.15)
    )
    return {
        "trend_score": round(_clip(trend_score), 4),
        "breadth_score": round(_clip(breadth_score), 4),
        "liquidity_score": round(_clip(liquidity_score), 4),
        "style_score": round(_clip(style_score), 4),
        "market_regime_score": round(_clip(market_regime_score), 4),
        "mechanism_scores": mech_scores,
    }
