from __future__ import annotations

from typing import Any, Dict, List


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def build_mechanism_candidates(event_cards: List[Dict[str, Any]], industry_router_payload: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    payload = dict(industry_router_payload or {})
    top_signals = [dict(item) for item in list(payload.get("top_stock_signals", []) or []) if isinstance(item, dict)]
    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for card in event_cards:
        direct_symbols = [str(item or "").strip().upper() for item in list(card.get("related_symbols", []) or []) if str(item or "").strip()]
        for symbol in direct_symbols:
            key = (card["event_id"], symbol)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "symbol": symbol,
                    "event_id": card["event_id"],
                    "mechanism_group": "direct_event_mapping",
                    "mechanism_fit_score": 1.0,
                    "mechanism_state_score": round(_clip(card.get("event_quality", 0.0) * 0.95 + 0.05), 4),
                    "signal_state": "direct",
                    "allow_entry_from_router": True,
                    "router_reason": "direct_event_symbol_mapping",
                    "event_quality": float(card.get("event_quality", 0.0)),
                    "primary_event_type": card.get("event_type", ""),
                    "event_summary": card.get("summary", ""),
                }
            )
        for item in top_signals[:12]:
            symbol = _text(item.get("symbol") or item.get("ts_code")).upper()
            if not symbol:
                continue
            key = (card["event_id"], symbol)
            if key in seen:
                continue
            fit = _clip(
                float(card.get("event_quality", 0.0)) * 0.45
                + _float(item.get("final_score", item.get("signal_score", 0.0))) * 0.40
                + (_float(item.get("state_score", 0.0)) * 0.15),
            )
            if fit < 0.22:
                continue
            seen.add(key)
            out.append(
                {
                    "symbol": symbol,
                    "event_id": card["event_id"],
                    "mechanism_group": _text(item.get("mechanism_primary") or item.get("mechanism_group") or "industry_router"),
                    "mechanism_fit_score": round(fit, 4),
                    "mechanism_state_score": round(_clip(_float(item.get("final_score", item.get("signal_score", 0.0)))), 4),
                    "signal_state": _text(item.get("signal_state") or "mapped"),
                    "allow_entry_from_router": bool(item.get("allow_entry", True)),
                    "router_reason": "industry_router_join",
                    "event_quality": float(card.get("event_quality", 0.0)),
                    "primary_event_type": card.get("event_type", ""),
                    "event_summary": card.get("summary", ""),
                }
            )
    return out

