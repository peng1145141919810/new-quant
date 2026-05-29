"""
Build a compact account + concentration snapshot for the trade clock heartbeat and intraday/T layers.

Reads broker health (`latest_account_health.json`) and OMS actual portfolio when available.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve())


def _oms_root(config: Dict[str, Any]) -> Path:
    raw = str(config.get("paths", {}).get("oms_output_root", "") or "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[3] / "data" / "live_execution_bridge" / "oms_v1"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def build_clock_account_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    snap_cfg = dict(config.get("trade_clock", {}).get("account_snapshot", {}) or {})
    if not bool(snap_cfg.get("enabled", True)):
        return {"enabled": False}

    health = _read_json(_trade_clock_root(config) / "latest_account_health.json")
    account_state = dict(health.get("account_state", {}) or {})
    nav = _float(account_state.get("nav") or account_state.get("total_asset"))
    cash = _float(account_state.get("cash") or account_state.get("available_cash"))
    positions_raw: List[Dict[str, Any]] = list(account_state.get("positions") or [])

    oms_actual = _read_json(_oms_root(config) / "snapshots" / "latest_actual_portfolio_state.json")
    if oms_actual:
        positions_raw = list(oms_actual.get("positions") or positions_raw or [])

    top1_high = float(snap_cfg.get("concentration_top1_high", 0.35) or 0.35)
    top1_elevated = float(snap_cfg.get("concentration_top1_elevated", 0.22) or 0.22)
    hhi_high = float(snap_cfg.get("concentration_hhi_high", 0.22) or 0.22)
    hhi_elevated = float(snap_cfg.get("concentration_hhi_elevated", 0.15) or 0.15)

    mvals: List[float] = []
    symbols: List[str] = []
    for row in positions_raw:
        mv = _float(row.get("market_value") or row.get("amount") or row.get("position_value"))
        if mv <= 0:
            continue
        sym = str(row.get("symbol") or row.get("ts_code") or row.get("code") or "").strip().upper()
        mvals.append(mv)
        symbols.append(sym)

    exposure = sum(mvals)
    weights = [mv / max(nav, 1e-9) for mv in mvals] if nav > 0 else []
    top1 = max(weights) if weights else 0.0
    hhi = float(sum(w * w for w in weights)) if weights else 0.0

    risk = "ok"
    if top1 >= top1_high or hhi >= hhi_high:
        risk = "high"
    elif top1 >= top1_elevated or hhi >= hhi_elevated:
        risk = "elevated"

    top_entries = sorted(
        [{"symbol": symbols[i], "market_value": mvals[i], "weight": weights[i]} for i in range(len(mvals))],
        key=lambda x: float(x.get("weight") or 0.0),
        reverse=True,
    )[:8]

    return {
        "enabled": True,
        "generated_from": "latest_account_health+oms_actual",
        "account_id": str(account_state.get("account_id") or health.get("account_id") or ""),
        "nav": round(nav, 2),
        "cash": round(cash, 2),
        "cash_ratio": round(cash / max(nav, 1e-9), 6) if nav > 0 else 0.0,
        "positions_count": len(positions_raw),
        "listed_positions_with_value": len(mvals),
        "exposure_market_value": round(exposure, 2),
        "exposure_ratio": round(exposure / max(nav, 1e-9), 6) if nav > 0 else 0.0,
        "concentration_top1_weight": round(top1, 6),
        "concentration_hhi": round(hhi, 6),
        "concentration_risk": risk,
        "heavy_position_warning": bool(risk == "high"),
        "top_positions": top_entries,
    }


def load_clock_account_snapshot_file(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _trade_clock_root(config) / "clock_account_snapshot.json"
    return _read_json(path)
