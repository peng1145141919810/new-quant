from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .intent_schema import INTENT_CLASS_PRIORITY, IntradayActionIntent, IntradayIntentConflict


def _score_intent(intent: IntradayActionIntent, symbol_ctx: Dict[str, Any], overlay: Dict[str, Any]) -> Tuple[Any, ...]:
    side = str(intent.side).upper()
    actual_weight = float(symbol_ctx.get("actual_weight", 0.0) or 0.0)
    buy_score = float(symbol_ctx.get("buy_timing_score", 0.0) or 0.0)
    sell_score = float(symbol_ctx.get("sell_timing_score", 0.0) or 0.0)
    constraint_score = float(symbol_ctx.get("timing_constraint_score", 1.0) or 1.0)
    concentration_risk = str(symbol_ctx.get("concentration_risk", "") or "").lower()
    panic_degrade_only = bool(overlay.get("panic_degrade_only", False))
    debug_payload = dict(getattr(intent, "debug_payload", {}) or {})
    outer_intraday = dict(debug_payload.get("outer_intelligence_intraday", {}) or {})
    outer_multiplier = float(outer_intraday.get("multiplier", 1.0) or 1.0)
    side_bias = 0
    if side == "SELL":
        side_bias = 0
        if concentration_risk == "high" and actual_weight >= 0.10:
            side_bias = -1
    elif panic_degrade_only:
        side_bias = 2
    signal_bias = -(sell_score if side == "SELL" else buy_score)
    if side == "BUY":
        signal_bias -= max(0.0, 1.0 - constraint_score) * 0.35
    return (
        int(INTENT_CLASS_PRIORITY.get(intent.intent_class, 99)),
        side_bias,
        signal_bias,
        -outer_multiplier,
        -float(intent.delta_shares),
    )


def arbitrate(
    intents: List[IntradayActionIntent],
    *,
    ctx: Dict[str, Any] | None = None,
) -> Tuple[List[IntradayActionIntent], List[IntradayIntentConflict], List[Dict[str, Any]]]:
    """Per symbol: keep the strongest context-aware intent and suppress the rest."""
    by_symbol: Dict[str, List[IntradayActionIntent]] = {}
    for it in intents:
        sym = str(it.symbol).strip().upper()
        by_symbol.setdefault(sym, []).append(it)

    symbol_rows: Dict[str, Dict[str, Any]] = {}
    overlay = {}
    if isinstance(ctx, dict):
        overlay = dict(dict(ctx.get("control_summary", {}) or {}).get("overlay_recommendation", {}) or {})
        frame = ctx.get("symbol_frame")
        if hasattr(frame, "iterrows"):
            for _, series in frame.iterrows():
                row = dict(series.to_dict())
                symbol = str(row.get("stock_code") or row.get("symbol") or row.get("ts_code") or "").strip().upper()
                if symbol:
                    symbol_rows[symbol] = row

    winners: List[IntradayActionIntent] = []
    conflicts: List[IntradayIntentConflict] = []
    suppressed_log: List[Dict[str, Any]] = []

    for sym, bucket in by_symbol.items():
        if len(bucket) == 1:
            winners.append(bucket[0])
            continue
        symbol_ctx = dict(symbol_rows.get(sym, {}) or {})
        ranked = sorted(
            bucket,
            key=lambda x: _score_intent(x, symbol_ctx, overlay),
        )
        w = ranked[0]
        losers = ranked[1:]
        suppressed = [x.intent_id for x in losers]
        winners.append(w)
        conflicts.append(
            IntradayIntentConflict(
                symbol=sym,
                winner_intent_id=w.intent_id,
                suppressed_intent_ids=suppressed,
                resolution="outer_context_priority",
                detail=f"winner_class={w.intent_class}; winner_side={w.side}; suppressed={len(suppressed)}",
            )
        )
        suppressed_log.append({"symbol": sym, "winner": w.intent_id, "suppressed": suppressed})

    return winners, conflicts, suppressed_log
