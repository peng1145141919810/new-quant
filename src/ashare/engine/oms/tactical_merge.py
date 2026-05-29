from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from live_execution_bridge.models import AccountState, OrderIntent
from live_execution_bridge.utils import safe_float


def _order_key(order: OrderIntent) -> Tuple[str, str]:
    return (str(order.symbol).strip().upper(), str(order.side).upper())


def merge_tactical_orders_into_control_result(
    control_result: Dict[str, Any],
    tactical_path: Path,
    account_state: AccountState,
    price_map: Dict[str, float],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Merge incremental tactical orders into portfolio_control final_orders (same symbol+side aggregates)."""
    audit: Dict[str, Any] = {"applied": False, "path": str(tactical_path), "n_tactical": 0, "merged_symbols": []}
    if not tactical_path.exists():
        return control_result, audit
    try:
        doc = json.loads(tactical_path.read_text(encoding="utf-8"))
    except Exception:
        return control_result, {**audit, "error": "read_failed"}

    orders_raw = list(doc.get("orders", []) or [])
    if not orders_raw:
        return control_result, audit

    merged: Dict[Tuple[str, str], OrderIntent] = {}
    for o in list(control_result.get("final_orders", []) or []):
        if not isinstance(o, OrderIntent):
            continue
        merged[_order_key(o)] = o

    positions = {str(p.symbol).upper(): p for p in account_state.positions}
    for row in orders_raw:
        symbol = str(row.get("symbol", "") or "").strip().upper()
        side = str(row.get("side", "") or "").strip().upper()
        ds = int(float(row.get("delta_shares", 0) or 0))
        if not symbol or side not in {"BUY", "SELL"} or ds <= 0:
            continue
        ref_price = safe_float(row.get("ref_price", 0.0), 0.0)
        if ref_price <= 0:
            pos = positions.get(symbol)
            ref_price = safe_float(price_map.get(symbol, pos.last_price if pos else 0.0), 0.0)
        reason = f"intraday_tactical|{row.get('reason_code', '')}|{row.get('intent_class', '')}"
        pos = positions.get(symbol)
        cur = int(pos.shares) if pos else 0
        tgt = cur + (ds if side == "BUY" else -ds)
        tactical_order = OrderIntent(
            symbol=symbol,
            side=side,
            target_shares=max(tgt, 0),
            delta_shares=ds,
            ref_price=float(ref_price),
            reason=reason[:512],
        )
        key = (symbol, side)
        if key in merged:
            prev = merged[key]
            merged[key] = OrderIntent(
                symbol=symbol,
                side=side,
                target_shares=max(prev.target_shares, tactical_order.target_shares),
                delta_shares=int(prev.delta_shares) + ds,
                ref_price=float(ref_price),
                reason=f"{prev.reason}|merged_tactical",
            )
        else:
            merged[key] = tactical_order
        audit["merged_symbols"].append(symbol)

    final_list: List[OrderIntent] = list(merged.values())
    out = dict(control_result)
    out["final_orders"] = final_list
    out["tactical_merge_audit"] = {
        **audit,
        "applied": True,
        "n_tactical": len(orders_raw),
        "n_final_orders": len(final_list),
    }
    return out, out["tactical_merge_audit"]
