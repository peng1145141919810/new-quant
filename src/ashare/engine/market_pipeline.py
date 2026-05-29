# -*- coding: utf-8 -*-
"""Market data sync, train-table append, and price snapshot utilities."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

import pandas as pd

from .config_utils import ensure_dir
from .logging_utils import log_line
from .research_fact_store import ensure_schema as ensure_research_fact_schema, insert_source_fetch_logs, resolve_research_fact_sqlite_path
from .sql_store import ensure_schema, fetch_enriched_history, resolve_sqlite_path, sql_store_enabled, sqlite_connection
from .tushare_client import TushareClient


ENRICHED_COLUMNS = [
    "code",
    "name",
    "adjust",
    "date",
    "open",
    "close",
    "high",
    "low",
    "amount",
    "pre_close",
    "pct_chg",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pb",
    "ps",
    "dv_ratio",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]

TRAIN_TABLE_COLUMNS = [
    "date",
    "code",
    "ts_code",
    "board",
    "industry",
    "listed_days",
    "in_hs300",
    "is_st",
    "is_suspended",
    "is_limit",
    "is_tradable_basic",
    "close",
    "pre_close",
    "pct_chg",
    "amount",
    "ret_1",
    "ret_5",
    "ret_10",
    "ret_20",
    "ret_60",
    "ret_120",
    "vol_5",
    "vol_20",
    "vol_60",
    "amount_mean_5",
    "amount_mean_20",
    "amount_z_20",
    "hs300_close",
    "hs300_ret_5",
    "hs300_ret_10",
    "hs300_ret_20",
    "hs300_ret_60",
    "alpha_ret_5_vs_hs300",
    "alpha_ret_10_vs_hs300",
    "alpha_ret_20_vs_hs300",
    "alpha_ret_60_vs_hs300",
    "future_ret_5",
    "future_ret_10",
    "future_ret_20",
    "year",
    "turnover_rate",
    "total_mv",
    "circ_mv",
    "turnover_mean_5",
    "turnover_mean_20",
]


def _market_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("market_pipeline", {}) or {})


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _normalize_ts_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text == "NAN":
        return ""
    if "." in text:
        return text
    code = _normalize_code(text)
    if not code:
        return ""
    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"{code}.SZ"
    if code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _to_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_market_fetch_log(
    *,
    run_id: str,
    dataset_name: str,
    source_name: str,
    started_at: datetime,
    finished_at: datetime,
    rows_written: int,
    message: str,
    publish_date: str = "",
) -> Dict[str, Any]:
    return {
        "log_id": f"market::{run_id}::{dataset_name}",
        "run_id": run_id,
        "pipeline_name": "market_pipeline",
        "dataset_name": dataset_name,
        "source_id": dataset_name,
        "source_name": source_name,
        "source_url": "",
        "source_domain": "",
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "publish_date": publish_date,
        "status": "success",
        "rows_written": int(rows_written),
        "items_seen": int(rows_written),
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
        "error_class": "",
        "message": message[:300],
        "artifact_path": "",
        "params_json": "",
        "extra_json": "",
        "is_stale": 0,
        "freshness_days": None,
    }


def _sql_path(config: Dict[str, Any]) -> Path:
    return resolve_sqlite_path(config)


@contextmanager
def _enriched_sql_write_conn(config: Dict[str, Any]) -> Iterator[Optional[sqlite3.Connection]]:
    if not sql_store_enabled(config):
        yield None
        return
    db_path = _sql_path(config)
    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        yield conn


def _upsert_enriched_rows_sql(
    config: Dict[str, Any], frame: pd.DataFrame, conn: Optional[sqlite3.Connection] = None
) -> int:
    if frame.empty or not sql_store_enabled(config):
        return 0
    db_path = _sql_path(config)
    rows = frame.copy()
    rows["code"] = rows["code"].map(_normalize_code)
    rows["ts_code"] = rows["code"].map(_normalize_ts_code)
    rows["trade_date"] = pd.to_datetime(rows["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    rows = rows.dropna(subset=["trade_date"])
    records = []
    for _, row in rows.iterrows():
        records.append(
            (
                row.get("code", ""),
                row.get("ts_code", ""),
                row.get("name", ""),
                row.get("adjust", "qfq"),
                row.get("trade_date", ""),
                row.get("open", ""),
                row.get("close", ""),
                row.get("high", ""),
                row.get("low", ""),
                row.get("amount", ""),
                row.get("pre_close", ""),
                row.get("pct_chg", ""),
                row.get("turnover_rate", ""),
                row.get("turnover_rate_f", ""),
                row.get("volume_ratio", ""),
                row.get("pe", ""),
                row.get("pb", ""),
                row.get("ps", ""),
                row.get("dv_ratio", ""),
                row.get("total_share", ""),
                row.get("float_share", ""),
                row.get("free_share", ""),
                row.get("total_mv", ""),
                row.get("circ_mv", ""),
            )
        )
    if not records:
        return 0

    def _executemany(target: sqlite3.Connection) -> int:
        ensure_schema(target)
        target.executemany(
            """
            INSERT INTO market_enriched_daily (
                code, ts_code, name, adjust, trade_date, open, close, high, low, amount,
                pre_close, pct_chg, turnover_rate, turnover_rate_f, volume_ratio, pe, pb, ps,
                dv_ratio, total_share, float_share, free_share, total_mv, circ_mv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                ts_code=excluded.ts_code,
                name=excluded.name,
                adjust=excluded.adjust,
                open=excluded.open,
                close=excluded.close,
                high=excluded.high,
                low=excluded.low,
                amount=excluded.amount,
                pre_close=excluded.pre_close,
                pct_chg=excluded.pct_chg,
                turnover_rate=excluded.turnover_rate,
                turnover_rate_f=excluded.turnover_rate_f,
                volume_ratio=excluded.volume_ratio,
                pe=excluded.pe,
                pb=excluded.pb,
                ps=excluded.ps,
                dv_ratio=excluded.dv_ratio,
                total_share=excluded.total_share,
                float_share=excluded.float_share,
                free_share=excluded.free_share,
                total_mv=excluded.total_mv,
                circ_mv=excluded.circ_mv
            """,
            records,
        )
        return len(records)

    if conn is not None:
        return _executemany(conn)
    with sqlite_connection(db_path) as owned:
        return _executemany(owned)


def _replace_hs300_rows_sql(
    config: Dict[str, Any], frame: pd.DataFrame, conn: Optional[sqlite3.Connection] = None
) -> int:
    if not sql_store_enabled(config):
        return 0
    rows = frame.copy()
    rows["trade_date"] = pd.to_datetime(rows["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    rows["close"] = pd.to_numeric(rows.get("close"), errors="coerce")
    rows = rows.dropna(subset=["trade_date"])
    records = [
        (
            str(row.get("trade_date", "")),
            row.get("close", ""),
            json.dumps(
                {
                    "trade_date": str(row.get("trade_date", "")),
                    "open": row.get("open", ""),
                    "high": row.get("high", ""),
                    "low": row.get("low", ""),
                    "close": row.get("close", ""),
                    "volume": row.get("volume", ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        for _, row in rows.iterrows()
    ]

    def _replace(target: sqlite3.Connection) -> int:
        ensure_schema(target)
        target.execute("DELETE FROM market_hs300_daily")
        if not records:
            return 0
        target.executemany(
            """
            INSERT INTO market_hs300_daily (trade_date, close, raw_json)
            VALUES (?, ?, ?)
            """,
            records,
        )
        return len(records)

    if conn is not None:
        return _replace(conn)
    with sqlite_connection(_sql_path(config)) as owned:
        return _replace(owned)


def _replace_price_snapshot_rows_sql(
    config: Dict[str, Any], frame: pd.DataFrame, conn: Optional[sqlite3.Connection] = None
) -> int:
    if not sql_store_enabled(config):
        return 0
    rows = frame.copy()
    rows["trade_date"] = pd.to_datetime(rows.get("date"), errors="coerce").dt.strftime("%Y-%m-%d")
    rows["code"] = rows["code"].map(_normalize_code)
    rows["ts_code"] = rows["ts_code"].map(_normalize_ts_code)
    rows["close"] = pd.to_numeric(rows.get("close"), errors="coerce")
    rows = rows.dropna(subset=["trade_date"])
    records = [
        (
            str(row.get("trade_date", "")),
            str(row.get("ts_code", "")),
            str(row.get("code", "")),
            row.get("close", ""),
            json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True, default=str),
        )
        for _, row in rows.iterrows()
        if str(row.get("ts_code", "")).strip()
    ]

    def _replace(target: sqlite3.Connection) -> int:
        ensure_schema(target)
        target.execute("DELETE FROM market_price_snapshot")
        if not records:
            return 0
        target.executemany(
            """
            INSERT INTO market_price_snapshot (trade_date, ts_code, code, close, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            records,
        )
        return len(records)

    if conn is not None:
        return _replace(conn)
    with sqlite_connection(_sql_path(config)) as owned:
        return _replace(owned)


def _load_recent_enriched_sql(config: Dict[str, Any], code: str, limit: int) -> pd.DataFrame:
    if not sql_store_enabled(config):
        return pd.DataFrame()
    db_path = _sql_path(config)
    if not db_path.exists():
        return pd.DataFrame()
    try:
        with sqlite_connection(db_path) as conn:
            return fetch_enriched_history(
                conn=conn,
                code=code,
                limit=limit,
                columns=[
                    "trade_date AS date",
                    "code",
                    "close",
                    "pre_close",
                    "pct_chg",
                    "amount",
                    "turnover_rate",
                    "total_mv",
                    "circ_mv",
                ],
            )
    except Exception:
        return pd.DataFrame()


def _load_stock_universe(path: Path) -> pd.DataFrame:
    cols = ["code", "name", "industry", "board", "last_date", "keep_flag", "ts_code"]
    df = pd.read_csv(path, usecols=lambda c: c in cols)
    df["code"] = df["code"].map(_normalize_code)
    if "ts_code" in df.columns:
        df["ts_code"] = df["ts_code"].map(_normalize_ts_code)
    else:
        df["ts_code"] = df["code"].map(_normalize_ts_code)
    if "last_date" in df.columns:
        df["last_date"] = _to_ts(df["last_date"])
    return df


def _load_listing_master(path: Path) -> pd.DataFrame:
    cols = ["ts_code", "code", "name", "industry", "board", "list_date"]
    df = pd.read_csv(path, usecols=lambda c: c in cols)
    df["code"] = df["code"].map(_normalize_code)
    df["ts_code"] = df["ts_code"].map(_normalize_ts_code)
    df["list_date"] = _to_ts(df["list_date"])
    return df.drop_duplicates("code", keep="last")


def _load_hs300_series(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["date", "close"])
    df["date"] = _to_ts(df["date"])
    df = df.dropna(subset=["date"]).sort_values("date").copy()
    df["hs300_close"] = pd.to_numeric(df["close"], errors="coerce")
    for horizon in [5, 10, 20, 60]:
        df[f"hs300_ret_{horizon}"] = df["hs300_close"] / df["hs300_close"].shift(horizon) - 1.0
    return df[["date", "hs300_close", "hs300_ret_5", "hs300_ret_10", "hs300_ret_20", "hs300_ret_60"]]


def _load_hs300_membership(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["date", "code", "in_hs300"])
    df["date"] = _to_ts(df["date"])
    df["code"] = df["code"].map(_normalize_code)
    return df.dropna(subset=["date"])


def _read_real_flags(path: Path, start_date: pd.Timestamp | None) -> pd.DataFrame:
    cols = [
        "date",
        "code",
        "ts_code",
        "board",
        "industry",
        "listed_days",
        "in_hs300",
        "is_st",
        "is_suspended",
        "is_limit_hit",
        "is_tradable_basic",
    ]
    frames: List[pd.DataFrame] = []
    dtype_map = {
        "code": "string",
        "ts_code": "string",
        "board": "string",
        "industry": "string",
    }
    for chunk in pd.read_csv(path, usecols=lambda c: c in cols, chunksize=300000, dtype=dtype_map, low_memory=False):
        chunk["date"] = _to_ts(chunk["date"])
        if start_date is not None:
            chunk = chunk.loc[chunk["date"] > start_date]
        if chunk.empty:
            continue
        chunk["code"] = chunk["code"].map(_normalize_code)
        chunk["ts_code"] = chunk["ts_code"].map(_normalize_ts_code)
        chunk = chunk.rename(columns={"is_limit_hit": "is_limit"})
        frames.append(chunk)
    if not frames:
        return pd.DataFrame(columns=["date", "code"])
    return pd.concat(frames, ignore_index=True)


def _load_latest_membership_map(path: Path) -> Dict[str, int]:
    df = _load_hs300_membership(path)
    if df.empty:
        return {}
    latest = df.sort_values(["code", "date"]).drop_duplicates("code", keep="last")
    return {str(row["code"]): int(row["in_hs300"]) for _, row in latest.iterrows()}


def _latest_trade_dates(client: TushareClient, start_date: str, end_date: str) -> List[str]:
    cal = client.call("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
    if cal.empty:
        return []
    cal["cal_date"] = cal["cal_date"].astype(str)
    cal = cal.loc[cal["is_open"].astype(str) == "1"].copy()
    return sorted(cal["cal_date"].tolist())


def _empty_snapshot_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "time",
            "code",
            "ts_code",
            "price",
            "close",
            "pre_close",
            "open",
            "high",
            "low",
            "vwap",
            "volume",
            "amount",
            "pct_chg",
            "turnover_rate",
            "total_mv",
            "circ_mv",
            "snapshot_source",
            "snapshot_quality",
        ]
    )


def _load_market_panel_from_tushare(client: TushareClient, trade_dates: Iterable[str]) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for trade_date in trade_dates:
        daily = client.call("daily", trade_date=str(trade_date))
        if daily.empty:
            continue
        basic = client.call("daily_basic", trade_date=str(trade_date))
        adj = client.call("adj_factor", trade_date=str(trade_date))
        merged = daily.merge(basic, on=["ts_code", "trade_date"], how="left", suffixes=("", "_basic"))
        merged = merged.merge(adj, on=["ts_code", "trade_date"], how="left")
        merged["trade_date"] = _to_ts(merged["trade_date"])
        merged["ts_code"] = merged["ts_code"].map(_normalize_ts_code)
        merged["code"] = merged["ts_code"].map(lambda x: x.split(".", 1)[0] if "." in x else _normalize_code(x))
        frames.append(merged)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _latest_available_market_panel(client: TushareClient, trade_dates: Iterable[str]) -> tuple[pd.DataFrame, str]:
    ordered = [str(item or "").strip() for item in list(trade_dates or []) if str(item or "").strip()]
    for trade_date in reversed(ordered):
        panel = _load_market_panel_from_tushare(client=client, trade_dates=[trade_date])
        if not panel.empty:
            return panel, trade_date
    return pd.DataFrame(), ""


def _normalize_snapshot_ts_code(value: Any) -> str:
    return _normalize_ts_code(value)


def _load_realtime_quote_frame(
    client: TushareClient,
    ts_codes: Iterable[str],
    *,
    src: str,
    batch_size: int,
) -> pd.DataFrame:
    batches: list[pd.DataFrame] = []
    codes = [str(item or "").strip().upper() for item in ts_codes if str(item or "").strip()]
    for start in range(0, len(codes), max(int(batch_size or 0), 1)):
        batch_codes = codes[start : start + max(int(batch_size or 0), 1)]
        frame = client.realtime_quote(ts_codes=batch_codes, src=src)
        if frame is None or frame.empty:
            continue
        batches.append(frame.copy())
    if not batches:
        return pd.DataFrame()
    out = pd.concat(batches, ignore_index=True)
    columns = {str(col).strip().upper(): col for col in out.columns}
    rename_map = {}
    for expected in ("TS_CODE", "DATE", "TIME", "OPEN", "PRE_CLOSE", "PRICE", "HIGH", "LOW", "VOLUME", "AMOUNT"):
        source_col = columns.get(expected)
        if source_col:
            rename_map[source_col] = expected.lower()
    out = out.rename(columns=rename_map)
    if "ts_code" not in out.columns:
        return pd.DataFrame()
    out["ts_code"] = out["ts_code"].map(_normalize_snapshot_ts_code)
    out = out.loc[out["ts_code"].astype(str) != ""].copy()
    if out.empty:
        return pd.DataFrame()
    out["code"] = out["ts_code"].map(lambda x: x.split(".", 1)[0] if "." in x else _normalize_code(x))
    for column in ("open", "pre_close", "price", "high", "low", "volume", "amount"):
        if column not in out.columns:
            out[column] = pd.NA
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if "date" not in out.columns:
        out["date"] = ""
    if "time" not in out.columns:
        out["time"] = ""
    out["date"] = out["date"].astype(str).str.replace("/", "-", regex=False).str.slice(0, 10)
    out["time"] = out["time"].astype(str).str.slice(0, 8)
    out["vwap"] = out["amount"] / out["volume"].replace(0, pd.NA)
    out["pct_chg"] = (out["price"] / out["pre_close"] - 1.0) * 100.0
    out["snapshot_source"] = f"tushare_realtime:{src}"
    out["snapshot_quality"] = "realtime_quote"
    out = out.sort_values(["ts_code"]).drop_duplicates(subset=["ts_code"], keep="last")
    return out[
        [
            "date",
            "time",
            "code",
            "ts_code",
            "open",
            "pre_close",
            "price",
            "high",
            "low",
            "volume",
            "amount",
            "vwap",
            "pct_chg",
            "snapshot_source",
            "snapshot_quality",
        ]
    ].copy()


def _sync_hs300_index(config: Dict[str, Any], client: TushareClient) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    hs300_path = Path(str(market_cfg["hs300_path"]))
    df = pd.read_csv(hs300_path)
    df["date"] = _to_ts(df["date"])
    last_date = df["date"].max()
    if pd.isna(last_date):
        updated_sql_rows = _replace_hs300_rows_sql(config=config, frame=df)
        return {"updated_rows": 0, "updated_sql_rows": int(updated_sql_rows)}
    trade_dates = _latest_trade_dates(
        client=client,
        start_date=(last_date + timedelta(days=1)).strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d"),
    )
    if not trade_dates:
        updated_sql_rows = _replace_hs300_rows_sql(config=config, frame=df)
        return {"updated_rows": 0, "updated_sql_rows": int(updated_sql_rows)}
    index_df = client.call("index_daily", ts_code="000300.SH", start_date=trade_dates[0], end_date=trade_dates[-1])
    if index_df.empty:
        updated_sql_rows = _replace_hs300_rows_sql(config=config, frame=df)
        return {"updated_rows": 0, "updated_sql_rows": int(updated_sql_rows)}
    index_df["date"] = _to_ts(index_df["trade_date"])
    append_df = index_df[["date", "open", "high", "low", "close", "vol"]].rename(columns={"vol": "volume"})
    merged = pd.concat([df, append_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    merged.to_csv(hs300_path, index=False, encoding="utf-8-sig")
    updated_sql_rows = _replace_hs300_rows_sql(config=config, frame=merged)
    return {"updated_rows": int(len(append_df)), "updated_sql_rows": int(updated_sql_rows)}


def sync_enriched_daily_from_tushare(config: Dict[str, Any], client: TushareClient) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    enriched_dir = Path(str(market_cfg["enriched_dir"]))
    stock_universe_path = Path(str(market_cfg["stock_universe_path"]))
    listing_path = Path(str(market_cfg["listing_master_path"]))
    if (not enriched_dir.exists()) or (not stock_universe_path.exists()) or (not listing_path.exists()):
        return {"updated_files": 0, "updated_rows": 0, "missing_trade_dates": []}

    universe = _load_stock_universe(stock_universe_path)
    listing = _load_listing_master(listing_path)
    listing_map = listing.set_index("ts_code").to_dict(orient="index")
    local_max_date = universe["last_date"].max()
    if pd.isna(local_max_date):
        return {"updated_files": 0, "updated_rows": 0, "missing_trade_dates": []}

    missing_trade_dates = _latest_trade_dates(
        client=client,
        start_date=(local_max_date + timedelta(days=1)).strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d"),
    )
    if not missing_trade_dates:
        return {"updated_files": 0, "updated_rows": 0, "missing_trade_dates": []}

    panel = _load_market_panel_from_tushare(client=client, trade_dates=missing_trade_dates)
    if panel.empty:
        return {"updated_files": 0, "updated_rows": 0, "missing_trade_dates": missing_trade_dates}

    anchor_adj = client.call("adj_factor", trade_date=local_max_date.strftime("%Y%m%d"))
    anchor_adj_map = {
        _normalize_ts_code(row["ts_code"]): float(row["adj_factor"])
        for _, row in anchor_adj.iterrows()
        if str(row.get("adj_factor", "")).strip()
    }
    universe_map = universe.set_index("ts_code").to_dict(orient="index")

    updated_rows = 0
    updated_files = 0
    updated_sql_rows = 0
    touched_dates: Dict[str, pd.Timestamp] = {}

    with _enriched_sql_write_conn(config) as sql_conn:
        for ts_code, g in panel.groupby("ts_code"):
            g = g.sort_values("trade_date").copy()
            code = _normalize_code(g["code"].iloc[0])
            if not code:
                continue
            meta = universe_map.get(ts_code) or listing_map.get(ts_code) or {}
            denom = float(anchor_adj_map.get(ts_code, 0.0) or 0.0)
            if denom <= 0:
                denom = float(pd.to_numeric(g["adj_factor"], errors="coerce").dropna().iloc[-1]) if g["adj_factor"].notna().any() else 1.0
            scale = pd.to_numeric(g["adj_factor"], errors="coerce").fillna(denom) / max(denom, 1e-9)
            out = pd.DataFrame(
                {
                    "code": code,
                    "name": str(meta.get("name", "") or ""),
                    "adjust": "qfq",
                    "date": g["trade_date"].dt.strftime("%Y-%m-%d"),
                    "open": pd.to_numeric(g["open"], errors="coerce") * scale,
                    "close": pd.to_numeric(g["close"], errors="coerce") * scale,
                    "high": pd.to_numeric(g["high"], errors="coerce") * scale,
                    "low": pd.to_numeric(g["low"], errors="coerce") * scale,
                    "amount": pd.to_numeric(g["amount"], errors="coerce"),
                    "pre_close": pd.to_numeric(g["pre_close"], errors="coerce") * scale,
                    "pct_chg": pd.to_numeric(g["pct_chg"], errors="coerce"),
                    "turnover_rate": pd.to_numeric(g.get("turnover_rate"), errors="coerce"),
                    "turnover_rate_f": pd.to_numeric(g.get("turnover_rate_f"), errors="coerce"),
                    "volume_ratio": pd.to_numeric(g.get("volume_ratio"), errors="coerce"),
                    "pe": pd.to_numeric(g.get("pe"), errors="coerce"),
                    "pb": pd.to_numeric(g.get("pb"), errors="coerce"),
                    "ps": pd.to_numeric(g.get("ps"), errors="coerce"),
                    "dv_ratio": pd.to_numeric(g.get("dv_ratio"), errors="coerce"),
                    "total_share": pd.to_numeric(g.get("total_share"), errors="coerce"),
                    "float_share": pd.to_numeric(g.get("float_share"), errors="coerce"),
                    "free_share": pd.to_numeric(g.get("free_share"), errors="coerce"),
                    "total_mv": pd.to_numeric(g.get("total_mv"), errors="coerce"),
                    "circ_mv": pd.to_numeric(g.get("circ_mv"), errors="coerce"),
                }
            )
            file_path = enriched_dir / f"{code}.csv"
            if file_path.exists():
                old = pd.read_csv(file_path)
                merged = pd.concat([old, out], ignore_index=True)
            else:
                merged = out
            merged["date"] = _to_ts(merged["date"])
            merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
            for col in ENRICHED_COLUMNS:
                if col not in merged.columns:
                    merged[col] = pd.NA
            merged = merged[ENRICHED_COLUMNS].copy()
            merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
            merged.to_csv(file_path, index=False, encoding="utf-8-sig")
            updated_sql_rows += _upsert_enriched_rows_sql(config=config, frame=out, conn=sql_conn)
            updated_rows += int(len(out))
            updated_files += 1
            touched_dates[ts_code] = _to_ts(pd.Series(out["date"])).max()

    if touched_dates:
        universe = universe.copy()
        mask = universe["ts_code"].isin(list(touched_dates.keys()))
        universe.loc[mask, "last_date"] = universe.loc[mask, "ts_code"].map(touched_dates)
        universe["last_date"] = universe["last_date"].dt.strftime("%Y-%m-%d")
        universe.to_csv(stock_universe_path, index=False, encoding="utf-8-sig")

    return {
        "updated_files": updated_files,
        "updated_rows": updated_rows,
        "updated_sql_rows": updated_sql_rows,
        "missing_trade_dates": missing_trade_dates,
    }


def build_daily_price_snapshot(config: Dict[str, Any], client: TushareClient) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    out_path = Path(str(market_cfg["price_snapshot_path"]))
    ensure_dir(out_path.parent)
    trade_dates = _latest_trade_dates(
        client=client,
        start_date=(datetime.now() - timedelta(days=10)).strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d"),
    )
    if not trade_dates:
        _replace_price_snapshot_rows_sql(config=config, frame=_empty_snapshot_frame())
        return {"path": str(out_path), "rows": 0, "trade_date": "", "updated_sql_rows": 0}
    panel, latest_trade_date = _latest_available_market_panel(client=client, trade_dates=trade_dates)
    if panel.empty:
        empty = _empty_snapshot_frame()
        empty.to_csv(out_path, index=False, encoding="utf-8-sig")
        _replace_price_snapshot_rows_sql(config=config, frame=empty)
        return {"path": str(out_path), "rows": 0, "trade_date": latest_trade_date, "snapshot_source": "", "snapshot_quality": "empty", "updated_sql_rows": 0}
    out = panel[
        ["trade_date", "code", "ts_code", "close", "pre_close", "pct_chg", "amount", "turnover_rate", "total_mv", "circ_mv"]
    ].copy()
    out = out.rename(columns={"trade_date": "date"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["time"] = ""
    out["code"] = out["code"].map(_normalize_code)
    out["price"] = pd.to_numeric(out["close"], errors="coerce")
    out["open"] = pd.NA
    out["high"] = pd.NA
    out["low"] = pd.NA
    out["vwap"] = pd.NA
    out["volume"] = pd.NA
    out["snapshot_source"] = f"tushare_daily:{latest_trade_date}"
    out["snapshot_quality"] = "daily_fallback"
    realtime_enabled = bool(market_cfg.get("realtime_quote_enabled", False))
    realtime_source = str(market_cfg.get("realtime_quote_source", "sina") or "sina")
    realtime_batch_size = int(market_cfg.get("realtime_quote_batch_size", 200) or 200)
    realtime_quote = (
        _load_realtime_quote_frame(
            client=client,
            ts_codes=out["ts_code"].tolist(),
            src=realtime_source,
            batch_size=realtime_batch_size,
        )
        if realtime_enabled
        else pd.DataFrame()
    )
    snapshot_source = str(out["snapshot_source"].iloc[0] if not out.empty else "")
    snapshot_quality = str(out["snapshot_quality"].iloc[0] if not out.empty else "")
    realtime_rows = 0
    if not realtime_quote.empty:
        realtime_rows = int(len(realtime_quote))
        out = out.merge(
            realtime_quote.add_prefix("rt_"),
            how="left",
            left_on="ts_code",
            right_on="rt_ts_code",
        )
        for column in ("date", "time", "open", "pre_close", "price", "high", "low", "volume", "amount", "vwap", "pct_chg"):
            rt_col = f"rt_{column}"
            if rt_col in out.columns:
                out[column] = out[rt_col].combine_first(out[column])
        if "rt_snapshot_source" in out.columns:
            out["snapshot_source"] = out["rt_snapshot_source"].combine_first(out["snapshot_source"])
        if "rt_snapshot_quality" in out.columns:
            out["snapshot_quality"] = out["rt_snapshot_quality"].combine_first(out["snapshot_quality"])
        out["close"] = out["price"].combine_first(out["close"])
        snapshot_source = str(out["snapshot_source"].iloc[0] if not out.empty else snapshot_source)
        snapshot_quality = str(out["snapshot_quality"].iloc[0] if not out.empty else snapshot_quality)
    out = out[
        [
            "date",
            "time",
            "code",
            "ts_code",
            "price",
            "close",
            "pre_close",
            "open",
            "high",
            "low",
            "vwap",
            "volume",
            "amount",
            "pct_chg",
            "turnover_rate",
            "total_mv",
            "circ_mv",
            "snapshot_source",
            "snapshot_quality",
        ]
    ].copy()
    out = out.sort_values("code")
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    updated_sql_rows = _replace_price_snapshot_rows_sql(config=config, frame=out)
    return {
        "path": str(out_path),
        "rows": int(len(out)),
        "trade_date": latest_trade_date,
        "snapshot_source": snapshot_source,
        "snapshot_quality": snapshot_quality,
        "realtime_rows": realtime_rows,
        "updated_sql_rows": int(updated_sql_rows),
    }


def _compute_train_max_date(train_dir: Path) -> pd.Timestamp | None:
    max_date: pd.Timestamp | None = None
    for file_path in sorted(train_dir.glob("*.parquet")):
        try:
            df = pd.read_parquet(file_path, columns=["date"])
        except Exception:
            continue
        if df.empty:
            continue
        cur = _to_ts(df["date"]).max()
        if pd.isna(cur):
            continue
        if max_date is None or cur > max_date:
            max_date = cur
    return max_date


def _compute_feature_rows(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["date"] = _to_ts(out["date"])
    out = out.sort_values("date").copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    amount = pd.to_numeric(out["amount"], errors="coerce")
    turnover = pd.to_numeric(out["turnover_rate"], errors="coerce")
    daily_ret = close.pct_change()
    out["ret_1"] = daily_ret
    for horizon in [5, 10, 20, 60, 120]:
        out[f"ret_{horizon}"] = close / close.shift(horizon) - 1.0
    for window in [5, 20, 60]:
        out[f"vol_{window}"] = daily_ret.rolling(window, min_periods=window).std()
    out["amount_mean_5"] = amount.rolling(5, min_periods=5).mean()
    out["amount_mean_20"] = amount.rolling(20, min_periods=20).mean()
    amount_std_20 = amount.rolling(20, min_periods=20).std()
    out["amount_z_20"] = (amount - out["amount_mean_20"]) / amount_std_20.replace(0, pd.NA)
    out["turnover_mean_5"] = turnover.rolling(5, min_periods=5).mean()
    out["turnover_mean_20"] = turnover.rolling(20, min_periods=20).mean()
    out["future_ret_5"] = close.shift(-5) / close - 1.0
    out["future_ret_10"] = close.shift(-10) / close - 1.0
    out["future_ret_20"] = close.shift(-20) / close - 1.0
    out["year"] = out["date"].dt.year
    return out


def _limit_threshold(board: str, code: str) -> float:
    board_upper = str(board or "").upper()
    if board_upper in {"STAR", "GEM"} or code.startswith(("300", "301", "688")):
        return 0.195
    if board_upper == "BSE" or code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return 0.295
    return 0.098


def append_train_table_incremental(config: Dict[str, Any]) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    train_dir = Path(str(config["train_table_dir"]))
    enriched_dir = Path(str(market_cfg["enriched_dir"]))
    flags_path = Path(str(market_cfg["flags_path"]))
    hs300_path = Path(str(market_cfg["hs300_path"]))
    hs300_membership_path = Path(str(market_cfg["hs300_membership_history_path"]))
    listing_path = Path(str(market_cfg["listing_master_path"]))
    stock_universe_path = Path(str(market_cfg["stock_universe_path"]))
    sql_ready = sql_store_enabled(config) and _sql_path(config).exists()
    if (not train_dir.exists()) or ((not enriched_dir.exists()) and (not sql_ready)) or (not hs300_path.exists()) or (not listing_path.exists()) or (not stock_universe_path.exists()):
        return {"appended_rows": 0, "output_path": ""}

    train_max_date = _compute_train_max_date(train_dir)
    if train_max_date is None:
        return {"appended_rows": 0, "output_path": ""}
    universe = _load_stock_universe(stock_universe_path)
    source_max_date = universe["last_date"].max()
    if pd.isna(source_max_date) or source_max_date <= train_max_date:
        return {"appended_rows": 0, "output_path": ""}

    hs300 = _load_hs300_series(hs300_path)
    listing = _load_listing_master(listing_path)
    listing_map = listing.set_index("code").to_dict(orient="index")
    latest_hs300_map = _load_latest_membership_map(hs300_membership_path)
    real_flags = _read_real_flags(flags_path, start_date=train_max_date) if flags_path.exists() else pd.DataFrame(columns=["date", "code"])

    lookback_rows = int(market_cfg.get("train_append_lookback_rows", 160) or 160)
    new_codes = universe.loc[universe["last_date"] > train_max_date, "code"].dropna().map(_normalize_code).unique().tolist()
    append_frames: List[pd.DataFrame] = []

    for code in new_codes:
        df = _load_recent_enriched_sql(config=config, code=code, limit=lookback_rows + 260)
        if df.empty:
            file_path = enriched_dir / f"{code}.csv"
            if not file_path.exists():
                continue
            try:
                df = pd.read_csv(
                    file_path,
                    usecols=[
                        "date",
                        "code",
                        "close",
                        "pre_close",
                        "pct_chg",
                        "amount",
                        "turnover_rate",
                        "total_mv",
                        "circ_mv",
                    ],
                )
            except Exception:
                continue
        if df.empty:
            continue
        df["date"] = _to_ts(df["date"])
        df["code"] = df["code"].map(_normalize_code)
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        if df["date"].max() <= train_max_date:
            continue
        first_new_idx = int(df.index[df["date"] > train_max_date][0])
        start_idx = max(0, first_new_idx - lookback_rows)
        work = df.iloc[start_idx:].copy()
        feat = _compute_feature_rows(work)
        feat = feat.loc[feat["date"] > train_max_date].copy()
        if feat.empty:
            continue
        append_frames.append(feat)

    if not append_frames:
        return {"appended_rows": 0, "output_path": ""}

    out = pd.concat(append_frames, ignore_index=True)
    out = out.merge(hs300, on="date", how="left")
    for horizon in [5, 10, 20, 60]:
        out[f"alpha_ret_{horizon}_vs_hs300"] = pd.to_numeric(out[f"ret_{horizon}"], errors="coerce") - pd.to_numeric(out[f"hs300_ret_{horizon}"], errors="coerce")

    if not real_flags.empty:
        merge_cols = ["date", "code", "ts_code", "board", "industry", "listed_days", "in_hs300", "is_st", "is_suspended", "is_limit", "is_tradable_basic"]
        out = out.merge(real_flags[merge_cols], on=["date", "code"], how="left")
    else:
        out["ts_code"] = pd.NA
        out["board"] = pd.NA
        out["industry"] = pd.NA
        out["listed_days"] = pd.NA
        out["in_hs300"] = pd.NA
        out["is_st"] = pd.NA
        out["is_suspended"] = pd.NA
        out["is_limit"] = pd.NA
        out["is_tradable_basic"] = pd.NA

    out["ts_code"] = out["ts_code"].fillna(out["code"].map(lambda x: listing_map.get(str(x), {}).get("ts_code", "")))
    out["board"] = out["board"].fillna(out["code"].map(lambda x: listing_map.get(str(x), {}).get("board", "")))
    out["industry"] = out["industry"].fillna(out["code"].map(lambda x: listing_map.get(str(x), {}).get("industry", "")))
    list_date_map = {code: meta.get("list_date") for code, meta in listing_map.items()}
    list_date_series = out["code"].map(list_date_map)
    out["listed_days"] = pd.to_numeric(out["listed_days"], errors="coerce")
    fallback_listed_days = (out["date"] - pd.to_datetime(list_date_series, errors="coerce")).dt.days
    out["listed_days"] = out["listed_days"].fillna(fallback_listed_days)
    out["in_hs300"] = pd.to_numeric(out["in_hs300"], errors="coerce").fillna(out["code"].map(lambda x: latest_hs300_map.get(str(x), 0)))
    name_map = {code: str(meta.get("name", "") or "") for code, meta in listing_map.items()}
    out["is_st"] = pd.to_numeric(out["is_st"], errors="coerce").fillna(out["code"].map(lambda x: 1 if "ST" in name_map.get(str(x), "").upper() else 0))
    out["is_suspended"] = pd.to_numeric(out["is_suspended"], errors="coerce").fillna((pd.to_numeric(out["amount"], errors="coerce").fillna(0.0) <= 0).astype(int))
    threshold_series = pd.Series(
        [_limit_threshold(board=str(board), code=str(code)) for board, code in zip(out["board"], out["code"])],
        index=out.index,
    )
    limit_fallback = (pd.to_numeric(out["pct_chg"], errors="coerce").abs() / 100.0 >= threshold_series).astype(int)
    out["is_limit"] = pd.to_numeric(out["is_limit"], errors="coerce").fillna(limit_fallback)
    tradable_fallback = (
        (pd.to_numeric(out["listed_days"], errors="coerce").fillna(0) >= 120)
        & (pd.to_numeric(out["is_st"], errors="coerce").fillna(0) == 0)
        & (pd.to_numeric(out["is_suspended"], errors="coerce").fillna(0) == 0)
        & (pd.to_numeric(out["is_limit"], errors="coerce").fillna(0) == 0)
    ).astype(int)
    out["is_tradable_basic"] = pd.to_numeric(out["is_tradable_basic"], errors="coerce").fillna(tradable_fallback)

    for col in ["listed_days", "in_hs300", "is_st", "is_suspended", "is_limit", "is_tradable_basic", "year"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["code"] = out["code"].map(_normalize_code)
    out = out.sort_values(["date", "code"]).drop_duplicates(subset=["date", "code"], keep="last")

    for col in TRAIN_TABLE_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[TRAIN_TABLE_COLUMNS].copy()

    out_path = train_dir / f"{str(market_cfg.get('train_append_prefix', 'part_live_append'))}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
    out.to_parquet(out_path, index=False)
    return {"appended_rows": int(len(out)), "output_path": str(out_path)}


def run_market_pipeline(config: Dict[str, Any]) -> Dict[str, Any]:
    market_cfg = _market_cfg(config)
    if not bool(market_cfg.get("enabled", False)):
        return {"enabled": False}

    report: Dict[str, Any] = {"enabled": True, "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    client = TushareClient(config.get("providers", {}).get("tushare", {}))
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    fetch_logs: List[Dict[str, Any]] = []

    if client.enabled() and bool(market_cfg.get("sync_tushare_missing_days", True)):
        try:
            hs300_started_at = datetime.now()
            sync_hs300 = _sync_hs300_index(config=config, client=client)
            hs300_finished_at = datetime.now()
            report["hs300_sync"] = sync_hs300
            fetch_logs.append(
                _build_market_fetch_log(
                    run_id=run_id,
                    dataset_name="hs300_index",
                    source_name="tushare.index_daily",
                    started_at=hs300_started_at,
                    finished_at=hs300_finished_at,
                    rows_written=int(sync_hs300.get("updated_rows", 0) or 0),
                    message="hs300 index sync",
                )
            )
            log_line(config, f"MarketPipeline: HS300 index sync done, updated_rows={sync_hs300.get('updated_rows', 0)}")
        except Exception as exc:
            report["hs300_sync_error"] = str(exc)
            log_line(config, f"MarketPipeline: HS300 index sync failed: {exc}")
        try:
            enriched_started_at = datetime.now()
            sync_report = sync_enriched_daily_from_tushare(config=config, client=client)
            enriched_finished_at = datetime.now()
            report["enriched_sync"] = sync_report
            missing_trade_dates = list(sync_report.get("missing_trade_dates", []) or [])
            fetch_logs.append(
                _build_market_fetch_log(
                    run_id=run_id,
                    dataset_name="market_enriched_daily",
                    source_name="tushare.daily+adj_factor+daily_basic",
                    started_at=enriched_started_at,
                    finished_at=enriched_finished_at,
                    rows_written=int(sync_report.get("updated_rows", 0) or 0),
                    message="enriched daily sync",
                    publish_date=str(missing_trade_dates[-1]) if missing_trade_dates else "",
                )
            )
            log_line(
                config,
                "MarketPipeline: enriched sync done, updated_files=%s updated_rows=%s missing_trade_dates=%s"
                % (
                    sync_report.get("updated_files", 0),
                    sync_report.get("updated_rows", 0),
                    len(missing_trade_dates),
                ),
            )
        except Exception as exc:
            report["enriched_sync_error"] = str(exc)
            log_line(config, f"MarketPipeline: enriched sync failed: {exc}")

        try:
            snapshot_started_at = datetime.now()
            price_snapshot = build_daily_price_snapshot(config=config, client=client)
            snapshot_finished_at = datetime.now()
            report["price_snapshot"] = price_snapshot
            fetch_logs.append(
                _build_market_fetch_log(
                    run_id=run_id,
                    dataset_name="market_price_snapshot",
                    source_name=str(price_snapshot.get("snapshot_source", "") or "tushare_realtime"),
                    started_at=snapshot_started_at,
                    finished_at=snapshot_finished_at,
                    rows_written=int(price_snapshot.get("rows", 0) or 0),
                    message=str(price_snapshot.get("snapshot_quality", "") or "price snapshot"),
                    publish_date=str(price_snapshot.get("trade_date", "") or ""),
                )
            )
            log_line(config, f"MarketPipeline: price snapshot done, trade_date={price_snapshot.get('trade_date', '')} rows={price_snapshot.get('rows', 0)}")
        except Exception as exc:
            report["price_snapshot_error"] = str(exc)
            log_line(config, f"MarketPipeline: price snapshot failed: {exc}")

    try:
        train_started_at = datetime.now()
        train_append = append_train_table_incremental(config=config)
        train_finished_at = datetime.now()
        report["train_table_append"] = train_append
        fetch_logs.append(
            _build_market_fetch_log(
                run_id=run_id,
                dataset_name="train_table_append",
                source_name="market_pipeline_append",
                started_at=train_started_at,
                finished_at=train_finished_at,
                rows_written=int(train_append.get("appended_rows", 0) or 0),
                message="train table incremental append",
            )
        )
        log_line(config, f"MarketPipeline: train append done, appended_rows={train_append.get('appended_rows', 0)}")
    except Exception as exc:
        report["train_table_append_error"] = str(exc)
        log_line(config, f"MarketPipeline: train append failed: {exc}")

    report["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_dir = ensure_dir(Path(str(config["paths"]["daily_cache_root"])))
    _write_json(out_dir / "market_pipeline_report.json", report)
    with sqlite_connection(resolve_research_fact_sqlite_path(config)) as ops_conn:
        ensure_research_fact_schema(ops_conn)
        insert_source_fetch_logs(ops_conn, fetch_logs)
    return report
