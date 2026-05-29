from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

try:
    from .config_builder import build_runtime_config
    from .sql_store import ensure_schema, resolve_sqlite_path, sqlite_connection
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from engine.config_builder import build_runtime_config
    from engine.sql_store import ensure_schema, resolve_sqlite_path, sqlite_connection


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        out = float(value)
        if pd.isna(out):
            return default
        return out
    except Exception:
        return default


def _code(value: Any) -> str:
    text = _text(value).upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def _json_payloads(conn: sqlite3.Connection, dataset: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM affordable_dataset_rows WHERE dataset = ?",
        (dataset,),
    ).fetchall()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(_text(row["payload_json"]))
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _stock_industry_map(affordable_conn: sqlite3.Connection) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for payload in _json_payloads(affordable_conn, "stock_basic"):
        code = _code(payload.get("ts_code") or payload.get("symbol"))
        if code:
            out[code] = _text(payload.get("industry"))
    return out


def _latest_by_code(payloads: Iterable[Dict[str, Any]], date_fields: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    latest_date: Dict[str, str] = {}
    for payload in payloads:
        code = _code(payload.get("ts_code") or payload.get("symbol"))
        if not code:
            continue
        date_text = ""
        for field in date_fields:
            date_text = _date(payload.get(field))
            if date_text:
                break
        if code not in latest or date_text >= latest_date.get(code, ""):
            latest[code] = payload
            latest_date[code] = date_text
    return latest


def _upsert(target: sqlite3.Connection, table: str, columns: List[str], rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(columns))
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in {"trade_date", "stock_code"})
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(trade_date, stock_code) DO UPDATE SET {updates}"
    )
    target.executemany(sql, [tuple(row.get(column) for column in columns) for row in rows])
    return len(rows)


def _build_valuation_rows(affordable_conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    industry_map = _stock_industry_map(affordable_conn)
    frame = pd.DataFrame(_json_payloads(affordable_conn, "daily_basic"))
    if frame.empty:
        return []
    frame["stock_code"] = frame.get("ts_code", "").map(_code)
    frame["trade_date"] = frame.get("trade_date", "").map(_date)
    frame["industry"] = frame["stock_code"].map(industry_map).fillna("")
    for column in ("pe_ttm", "pb", "ps_ttm"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame = frame.loc[frame["stock_code"].ne("") & frame["trade_date"].ne("")]
    if frame.empty:
        return []
    for source, target in (("pe_ttm", "pe_pct_industry"), ("pb", "pb_pct_industry"), ("ps_ttm", "ps_pct_industry")):
        frame[target] = (
            frame.groupby(["trade_date", "industry"], dropna=False)[source]
            .rank(pct=True, method="average")
            .fillna(0.0)
        )
        frame[target.replace("_industry", "_1y")] = (
            frame.groupby("stock_code", dropna=False)[source]
            .rank(pct=True, method="average")
            .fillna(0.0)
        )
    rows: List[Dict[str, Any]] = []
    for _, row in frame.iterrows():
        payload = row.to_dict()
        rows.append(
            {
                "trade_date": row["trade_date"],
                "stock_code": row["stock_code"],
                "pe_ttm": _float(row.get("pe_ttm")),
                "pb": _float(row.get("pb")),
                "ps_ttm": _float(row.get("ps_ttm")),
                "ev_ebitda": None,
                "pe_pct_1y": _float(row.get("pe_pct_1y"), 0.0),
                "pb_pct_1y": _float(row.get("pb_pct_1y"), 0.0),
                "ps_pct_1y": _float(row.get("ps_pct_1y"), 0.0),
                "pe_pct_industry": _float(row.get("pe_pct_industry"), 0.0),
                "pb_pct_industry": _float(row.get("pb_pct_industry"), 0.0),
                "ps_pct_industry": _float(row.get("ps_pct_industry"), 0.0),
                "raw_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            }
        )
    return rows


def _build_crowding_rows(affordable_conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    daily = pd.DataFrame(_json_payloads(affordable_conn, "daily_basic"))
    if daily.empty:
        return []
    daily["stock_code"] = daily.get("ts_code", "").map(_code)
    daily["trade_date"] = daily.get("trade_date", "").map(_date)
    for column in ("turnover_rate", "turnover_rate_f", "volume_ratio"):
        daily[column] = pd.to_numeric(daily.get(column), errors="coerce")
    daily = daily.loc[daily["stock_code"].ne("") & daily["trade_date"].ne("")]
    if daily.empty:
        return []

    hk_hold = pd.DataFrame(_json_payloads(affordable_conn, "hk_hold"))
    if not hk_hold.empty:
        hk_hold["stock_code"] = hk_hold.get("ts_code", "").map(_code)
        hk_hold["trade_date"] = hk_hold.get("trade_date", "").map(_date)
        hk_hold["northbound_holding"] = pd.to_numeric(hk_hold.get("vol"), errors="coerce")
        hk_hold = hk_hold.sort_values(["stock_code", "trade_date"])
        hk_hold["northbound_holding_change"] = hk_hold.groupby("stock_code")["northbound_holding"].diff().fillna(0.0)
        daily = daily.merge(
            hk_hold[["stock_code", "trade_date", "northbound_holding", "northbound_holding_change"]],
            on=["stock_code", "trade_date"],
            how="left",
        )

    margin = pd.DataFrame(_json_payloads(affordable_conn, "margin_detail"))
    if not margin.empty:
        margin["stock_code"] = margin.get("ts_code", "").map(_code)
        margin["trade_date"] = margin.get("trade_date", "").map(_date)
        margin["margin_balance"] = pd.to_numeric(margin.get("rzye"), errors="coerce")
        margin = margin.sort_values(["stock_code", "trade_date"])
        margin["margin_balance_change"] = margin.groupby("stock_code")["margin_balance"].diff().fillna(0.0)
        daily = daily.merge(
            margin[["stock_code", "trade_date", "margin_balance", "margin_balance_change"]],
            on=["stock_code", "trade_date"],
            how="left",
        )

    daily["turnover_pct_rank"] = daily.groupby("trade_date")["turnover_rate"].rank(pct=True, method="average").fillna(0.0)
    daily["fund_exposure_proxy"] = daily.groupby("trade_date")["volume_ratio"].rank(pct=True, method="average").fillna(0.0)
    daily["crowding_score"] = (
        daily["turnover_pct_rank"] * 0.55
        + daily["fund_exposure_proxy"] * 0.25
        + daily.groupby("trade_date")["margin_balance_change"].rank(pct=True, method="average").fillna(0.0) * 0.20
    ).clip(lower=0.0, upper=1.0)
    rows: List[Dict[str, Any]] = []
    for _, row in daily.iterrows():
        rows.append(
            {
                "trade_date": row["trade_date"],
                "stock_code": row["stock_code"],
                "turnover_rate": _float(row.get("turnover_rate")),
                "turnover_pct_rank": _float(row.get("turnover_pct_rank"), 0.0),
                "northbound_holding": _float(row.get("northbound_holding")),
                "northbound_holding_change": _float(row.get("northbound_holding_change")),
                "margin_balance": _float(row.get("margin_balance")),
                "margin_balance_change": _float(row.get("margin_balance_change")),
                "fund_exposure_proxy": _float(row.get("fund_exposure_proxy"), 0.0),
                "crowding_score": _float(row.get("crowding_score"), 0.0),
                "raw_json": json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True, default=str),
            }
        )
    return rows


def _build_expectation_rows(affordable_conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    forecasts = _latest_by_code(_json_payloads(affordable_conn, "forecast"), ("ann_date", "end_date"))
    express = _latest_by_code(_json_payloads(affordable_conn, "express"), ("ann_date", "end_date"))
    codes = sorted(set(forecasts) | set(express))
    if not codes:
        return []
    raw_revisions: Dict[str, float] = {}
    source_dates: Dict[str, str] = {}
    payloads: Dict[str, Dict[str, Any]] = {}
    for code in codes:
        forecast = forecasts.get(code, {})
        expr = express.get(code, {})
        revision = _float(forecast.get("p_change")) or _float(forecast.get("net_profit_change"))
        if revision is None:
            revision = _float(expr.get("yoy_net_profit")) or _float(expr.get("yoy_sales"))
        if revision is None:
            continue
        raw_revisions[code] = max(-200.0, min(300.0, revision))
        source_dates[code] = max(_date(forecast.get("ann_date")), _date(expr.get("ann_date")))
        payloads[code] = {"forecast": forecast, "express": expr}
    if not raw_revisions:
        return []
    values = pd.Series(raw_revisions, dtype="float64")
    lo = float(values.min())
    hi = float(values.max())
    if hi - lo <= 1e-9:
        scores = pd.Series({code: 0.0 for code in raw_revisions})
    else:
        scores = ((values - lo) / (hi - lo)).clip(lower=0.0, upper=1.0)
    rows: List[Dict[str, Any]] = []
    for code, revision in raw_revisions.items():
        trade_date = source_dates.get(code) or datetime.now().strftime("%Y-%m-%d")
        rows.append(
            {
                "trade_date": trade_date,
                "stock_code": code,
                "eps_fy1": None,
                "eps_fy2": None,
                "eps_revision_7d": revision / 100.0,
                "eps_revision_30d": revision / 100.0,
                "analyst_count": 1.0,
                "revision_score": float(scores.get(code, 0.0)),
                "raw_json": json.dumps(payloads.get(code, {}), ensure_ascii=False, sort_keys=True, default=str),
            }
        )
    return rows


def run_derived_alpha_refresh(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(config.get("derived_alpha_refresh", {}) or {})
    if not bool(cfg.get("enabled", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    paths = dict(config.get("paths", {}) or {})
    affordable_path = Path(_text(cfg.get("affordable_sqlite_path") or paths.get("affordable_sqlite_path"))).resolve()
    runtime_path = Path(_text(cfg.get("runtime_sqlite_path") or resolve_sqlite_path(config))).resolve()
    if not affordable_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_affordable_db:{affordable_path}"}
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(str(affordable_path)) as affordable_conn:
        affordable_conn.row_factory = sqlite3.Row
        valuation_rows = _build_valuation_rows(affordable_conn)
        crowding_rows = _build_crowding_rows(affordable_conn)
        expectation_rows = _build_expectation_rows(affordable_conn)
    with sqlite_connection(runtime_path) as runtime_conn:
        ensure_schema(runtime_conn)
        valuation_count = _upsert(
            runtime_conn,
            "valuation_daily",
            [
                "trade_date",
                "stock_code",
                "pe_ttm",
                "pb",
                "ps_ttm",
                "ev_ebitda",
                "pe_pct_1y",
                "pb_pct_1y",
                "ps_pct_1y",
                "pe_pct_industry",
                "pb_pct_industry",
                "ps_pct_industry",
                "raw_json",
            ],
            valuation_rows,
        )
        crowding_count = _upsert(
            runtime_conn,
            "crowding_daily",
            [
                "trade_date",
                "stock_code",
                "turnover_rate",
                "turnover_pct_rank",
                "northbound_holding",
                "northbound_holding_change",
                "margin_balance",
                "margin_balance_change",
                "fund_exposure_proxy",
                "crowding_score",
                "raw_json",
            ],
            crowding_rows,
        )
        expectation_count = _upsert(
            runtime_conn,
            "expectation_revision_daily",
            [
                "trade_date",
                "stock_code",
                "eps_fy1",
                "eps_fy2",
                "eps_revision_7d",
                "eps_revision_30d",
                "analyst_count",
                "revision_score",
                "raw_json",
            ],
            expectation_rows,
        )
    return {
        "enabled": True,
        "ran": True,
        "ok": True,
        "message": "ok",
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "affordable_sqlite_path": str(affordable_path),
        "runtime_sqlite_path": str(runtime_path),
        "rows_written": {
            "valuation_daily": int(valuation_count),
            "crowding_daily": int(crowding_count),
            "expectation_revision_daily": int(expectation_count),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh runtime alpha feature tables from the affordable SQLite store.")
    parser.add_argument("--config", default="", help="Optional runtime config JSON path.")
    parser.add_argument("--affordable-db", default="", help="Override affordable SQLite path.")
    parser.add_argument("--runtime-db", default="", help="Override runtime research SQLite path.")
    args = parser.parse_args()
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    else:
        config = build_runtime_config()
    if args.affordable_db:
        config.setdefault("derived_alpha_refresh", {})["affordable_sqlite_path"] = str(Path(args.affordable_db).resolve())
    if args.runtime_db:
        config.setdefault("derived_alpha_refresh", {})["runtime_sqlite_path"] = str(Path(args.runtime_db).resolve())
    result = run_derived_alpha_refresh(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if bool(result.get("ok", False)) else 1


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        __package__ = "engine"
    raise SystemExit(main())
