from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _payload(conn: sqlite3.Connection, dataset: str, symbol: str) -> Dict[str, Any]:
    ts_code = symbol if "." in symbol else ""
    code = symbol.split(".", 1)[0] if "." in symbol else symbol
    row = conn.execute(
        """
        select payload_json
        from affordable_dataset_rows
        where dataset = ? and (upper(ts_code) = upper(?) or upper(symbol) = upper(?))
        order by primary_date desc, secondary_date desc, updated_at desc
        limit 1
        """,
        (dataset, ts_code, code),
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return dict(json.loads(row[0]))
    except Exception:
        return {}


def build_earnings_validation(config: Dict[str, Any], symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    sqlite_path = Path(str(config.get("paths", {}).get("affordable_sqlite_path", "") or "")).resolve()
    if not sqlite_path.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    conn = sqlite3.connect(str(sqlite_path))
    try:
        for symbol in {str(item or "").strip().upper() for item in symbols if str(item or "").strip()}:
            forecast = _payload(conn, "forecast", symbol)
            express = _payload(conn, "express", symbol)
            daily_basic = _payload(conn, "daily_basic", symbol)
            expectation = _payload(conn, "internal_expectation", symbol)
            score = 0.0
            reason: list[str] = []
            revision_support = 0.0
            if forecast:
                score += 0.28
                pmax = _float(forecast.get("p_change_max", 0.0))
                pmin = _float(forecast.get("p_change_min", 0.0))
                if max(pmax, pmin) > 0:
                    score += 0.10
                    reason.append("forecast_support_positive")
                if abs(max(pmax, pmin, key=abs)) >= 20:
                    revision_support += 0.16
                    reason.append("forecast_revision_significant")
            if express:
                score += 0.22
                yoy = _float(express.get("yoy_net_profit", 0.0))
                if yoy > 0:
                    score += 0.08
                    reason.append("express_profit_positive")
                if abs(yoy) >= 20:
                    revision_support += 0.14
                    reason.append("express_change_significant")
            if expectation:
                score += 0.20
                confidence = _float(expectation.get("confidence", 0.0))
                score += 0.10 * _clip(confidence)
                source_mix = _text(expectation.get("source_mix"))
                if source_mix:
                    reason.append(f"expectation:{source_mix}")
                revision_support += 0.10 * _clip(abs(_float(expectation.get("revision_ratio", 0.0))), 0.0, 1.0)
            pe_ttm = _float(daily_basic.get("pe_ttm", daily_basic.get("pe", 0.0)))
            pb = _float(daily_basic.get("pb", 0.0))
            valuation_penalty = 0.0
            if pe_ttm > 35:
                valuation_penalty += 0.08
            elif pe_ttm > 20:
                valuation_penalty += 0.04
            if pb > 5:
                valuation_penalty += 0.04
            implementation_risk = 0.12 if not (forecast or express or expectation) else 0.04
            validation = _clip(score - valuation_penalty - implementation_risk)
            earnings_gate_pass = bool(validation >= 0.28 and (forecast or express or expectation))
            out[symbol] = {
                "symbol": symbol,
                "earnings_validation_score": round(validation, 4),
                "earnings_confidence": round(_clip(score), 4),
                "revision_support": round(_clip(revision_support), 4),
                "expected_profit_proxy": _float(expectation.get("expected_profit", 0.0)),
                "revision_ratio": _float(expectation.get("revision_ratio", 0.0)),
                "growth_proxy": _float(expectation.get("growth_proxy", 0.0)),
                "source_mix": _text(expectation.get("source_mix")),
                "valuation_penalty": round(valuation_penalty, 4),
                "implementation_risk": round(implementation_risk, 4),
                "earnings_gate_pass": earnings_gate_pass,
                "earnings_reason_chain": reason,
                "earnings_reason": ";".join(reason)[:220],
            }
    finally:
        conn.close()
    return out
