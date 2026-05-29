from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "ashare"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from engine.config_builder import build_runtime_config
from engine.research_fact_store import (
    ensure_schema as ensure_research_fact_schema,
    insert_source_fetch_logs,
    resolve_research_fact_sqlite_path,
    sqlite_connection as research_fact_sqlite_connection,
)
from engine.tushare_client import TushareClient


DEFAULT_DB_PATH = REPO_ROOT / "data" / "sql_store" / "affordable_data_v1.sqlite3"
DEFAULT_SNAPSHOT_ROOT = REPO_ROOT / "data" / "affordable_feeds" / "latest"
DEFAULT_DAILY_LOOKBACK = 3
DEFAULT_ANNOUNCEMENT_LOOKBACK = 30


def _load_customs_helper() -> tuple[list[str], Callable[[str], Dict[str, Any]]]:
    module_path = REPO_ROOT / "tools" / "fetch_customs_summary_gov_cn.py"
    spec = importlib.util.spec_from_file_location("fetch_customs_summary_gov_cn", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load customs helper from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    urls = list(getattr(module, "DEFAULT_URLS", []) or [])
    fetch_one = getattr(module, "fetch_one", None)
    if not callable(fetch_one):
        raise RuntimeError("customs helper does not expose fetch_one")
    return urls, fetch_one


CUSTOMS_DEFAULT_URLS: list[str] | None = None
FETCH_CUSTOMS_ONE: Callable[[str], Dict[str, Any]] | None = None
UNVERIFIED_SSL_CONTEXT = ssl._create_unverified_context()


def _ensure_customs_helper() -> tuple[list[str], Callable[[str], Dict[str, Any]]]:
    global CUSTOMS_DEFAULT_URLS, FETCH_CUSTOMS_ONE
    if CUSTOMS_DEFAULT_URLS is None or FETCH_CUSTOMS_ONE is None:
        CUSTOMS_DEFAULT_URLS, FETCH_CUSTOMS_ONE = _load_customs_helper()
    return list(CUSTOMS_DEFAULT_URLS or []), FETCH_CUSTOMS_ONE


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source_name: str
    mode: str
    api_name: str = ""
    key_fields: tuple[str, ...] = ()
    primary_date_fields: tuple[str, ...] = ()
    secondary_date_fields: tuple[str, ...] = ()
    default_enabled: bool = True
    requires_ts_codes: bool = False


DATASET_SPECS: Dict[str, DatasetSpec] = {
    "stock_basic": DatasetSpec(
        name="stock_basic",
        source_name="tushare",
        mode="stock_basic_snapshot",
        api_name="stock_basic",
        key_fields=("ts_code",),
    ),
    "daily": DatasetSpec(
        name="daily",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="daily",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "adj_factor": DatasetSpec(
        name="adj_factor",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="adj_factor",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "daily_basic": DatasetSpec(
        name="daily_basic",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="daily_basic",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "forecast": DatasetSpec(
        name="forecast",
        source_name="tushare",
        mode="ann_date_loop",
        api_name="forecast",
        key_fields=("ts_code", "ann_date", "end_date", "type"),
        primary_date_fields=("ann_date",),
        secondary_date_fields=("end_date",),
    ),
    "express": DatasetSpec(
        name="express",
        source_name="tushare",
        mode="ann_date_loop",
        api_name="express",
        key_fields=("ts_code", "ann_date", "end_date"),
        primary_date_fields=("ann_date",),
        secondary_date_fields=("end_date",),
    ),
    "dividend": DatasetSpec(
        name="dividend",
        source_name="tushare",
        mode="ann_date_loop",
        api_name="dividend",
        key_fields=("ts_code", "ann_date", "end_date", "div_proc"),
        primary_date_fields=("ann_date",),
        secondary_date_fields=("end_date",),
    ),
    "stk_holdertrade": DatasetSpec(
        name="stk_holdertrade",
        source_name="tushare",
        mode="ann_date_loop",
        api_name="stk_holdertrade",
        key_fields=("ts_code", "ann_date", "holder_name", "in_de"),
        primary_date_fields=("ann_date",),
    ),
    "ggt_daily": DatasetSpec(
        name="ggt_daily",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="ggt_daily",
        key_fields=("trade_date",),
        primary_date_fields=("trade_date",),
    ),
    "moneyflow_hsgt": DatasetSpec(
        name="moneyflow_hsgt",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="moneyflow_hsgt",
        key_fields=("trade_date",),
        primary_date_fields=("trade_date",),
    ),
    "hk_hold": DatasetSpec(
        name="hk_hold",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="hk_hold",
        key_fields=("ts_code", "trade_date", "exchange"),
        primary_date_fields=("trade_date",),
    ),
    "margin": DatasetSpec(
        name="margin",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="margin",
        key_fields=("trade_date", "exchange_id"),
        primary_date_fields=("trade_date",),
    ),
    "margin_detail": DatasetSpec(
        name="margin_detail",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="margin_detail",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "moneyflow": DatasetSpec(
        name="moneyflow",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="moneyflow",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "stk_limit": DatasetSpec(
        name="stk_limit",
        source_name="tushare",
        mode="trade_date_loop",
        api_name="stk_limit",
        key_fields=("ts_code", "trade_date"),
        primary_date_fields=("trade_date",),
    ),
    "fina_indicator": DatasetSpec(
        name="fina_indicator",
        source_name="tushare",
        mode="ts_code_loop",
        api_name="fina_indicator",
        key_fields=("ts_code", "ann_date", "end_date"),
        primary_date_fields=("ann_date",),
        secondary_date_fields=("end_date",),
        default_enabled=False,
        requires_ts_codes=True,
    ),
    "customs_summary": DatasetSpec(
        name="customs_summary",
        source_name="gov_cn",
        mode="customs_urls",
        key_fields=("source_url",),
        primary_date_fields=("release_date",),
    ),
    "internal_expectation": DatasetSpec(
        name="internal_expectation",
        source_name="local_model",
        mode="internal_expectation",
        key_fields=("ts_code", "as_of_date"),
        primary_date_fields=("as_of_date",),
    ),
    "ccgp_bid_awards": DatasetSpec(
        name="ccgp_bid_awards",
        source_name="ccgp",
        mode="ccgp_bid_awards",
        key_fields=("source_url",),
        primary_date_fields=("published_at",),
    ),
    "ppi_market_digest": DatasetSpec(
        name="ppi_market_digest",
        source_name="100ppi",
        mode="ppi_market_digest",
        key_fields=("source_url",),
        primary_date_fields=("as_of_date",),
    ),
}


def log_event(started_at: float, event: str, **kwargs: Any) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed = time.monotonic() - started_at
    detail = ", ".join(f"{key}={value}" for key, value in kwargs.items())
    suffix = f": {detail}" if detail else ""
    print(f"[{timestamp}] [{elapsed:8.1f}s] {event}{suffix}")


def sqlite_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS affordable_dataset_rows (
            dataset TEXT NOT NULL,
            source_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            primary_date TEXT,
            secondary_date TEXT,
            ts_code TEXT,
            symbol TEXT,
            name TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (dataset, record_key)
        );
        CREATE TABLE IF NOT EXISTS affordable_source_runs (
            run_id TEXT NOT NULL,
            dataset TEXT NOT NULL,
            source_name TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            params_json TEXT NOT NULL,
            message TEXT,
            PRIMARY KEY (run_id, dataset)
        );
        CREATE INDEX IF NOT EXISTS idx_affordable_rows_dataset_primary_date
            ON affordable_dataset_rows (dataset, primary_date);
        CREATE INDEX IF NOT EXISTS idx_affordable_rows_dataset_ts_code
            ON affordable_dataset_rows (dataset, ts_code);
        CREATE INDEX IF NOT EXISTS idx_affordable_runs_dataset_started_at
            ON affordable_source_runs (dataset, started_at);
        """
    )
    conn.commit()


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    payload = list(rows)
    if not payload:
        return 0
    sql = """
        INSERT INTO affordable_dataset_rows (
            dataset,
            source_name,
            record_key,
            primary_date,
            secondary_date,
            ts_code,
            symbol,
            name,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset, record_key) DO UPDATE SET
            source_name=excluded.source_name,
            primary_date=excluded.primary_date,
            secondary_date=excluded.secondary_date,
            ts_code=excluded.ts_code,
            symbol=excluded.symbol,
            name=excluded.name,
            payload_json=excluded.payload_json,
            updated_at=CURRENT_TIMESTAMP
    """
    conn.executemany(
        sql,
        [
            (
                item["dataset"],
                item["source_name"],
                item["record_key"],
                item.get("primary_date", ""),
                item.get("secondary_date", ""),
                item.get("ts_code", ""),
                item.get("symbol", ""),
                item.get("name", ""),
                item["payload_json"],
            )
            for item in payload
        ],
    )
    conn.commit()
    return len(payload)


def write_run_record(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    dataset: str,
    source_name: str,
    status: str,
    row_count: int,
    started_at: datetime,
    finished_at: datetime | None,
    params: Dict[str, Any],
    message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO affordable_source_runs (
            run_id, dataset, source_name, status, row_count, started_at, finished_at, params_json, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, dataset) DO UPDATE SET
            source_name=excluded.source_name,
            status=excluded.status,
            row_count=excluded.row_count,
            started_at=excluded.started_at,
            finished_at=excluded.finished_at,
            params_json=excluded.params_json,
            message=excluded.message
        """,
        (
            run_id,
            dataset,
            source_name,
            status,
            int(row_count),
            started_at.isoformat(timespec="seconds"),
            finished_at.isoformat(timespec="seconds") if finished_at else "",
            json.dumps(params, ensure_ascii=False, sort_keys=True),
            message,
        ),
    )
    conn.commit()


def build_source_fetch_log(
    *,
    run_id: str,
    result: Dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
) -> Dict[str, Any]:
    dataset = str(result.get("dataset", "") or "")
    params = dict(result.get("params", {}) or {})
    message = str(result.get("message", "") or "")
    source_url = ""
    if dataset == "customs_summary":
        source_url = "|".join(str(item) for item in list(params.get("urls", []) or [])[:3])
    return {
        "log_id": f"affordable::{run_id}::{dataset}",
        "run_id": run_id,
        "pipeline_name": "affordable_data_refresh",
        "dataset_name": dataset,
        "source_id": dataset,
        "source_name": str(result.get("source_name", "") or ""),
        "source_url": source_url,
        "source_domain": "",
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "publish_date": str(result.get("latest_primary_date", "") or ""),
        "status": str(result.get("status", "") or "unknown"),
        "rows_written": int(result.get("rows_written", 0) or 0),
        "items_seen": int(result.get("frame_rows", 0) or 0),
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
        "error_class": "" if str(result.get("status")) == "success" else "dataset_failed",
        "message": message[:300],
        "artifact_path": message if message.startswith("snapshot=") else "",
        "params_json": json.dumps(params, ensure_ascii=False),
        "extra_json": "",
        "is_stale": 0,
        "freshness_days": None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update the affordable data bundle into a standalone SQLite store.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Target SQLite path.")
    parser.add_argument("--snapshot-root", default=str(DEFAULT_SNAPSHOT_ROOT), help="CSV snapshot output root.")
    parser.add_argument("--dataset", action="append", default=[], help="Dataset name, comma-separated names, or 'all'.")
    parser.add_argument("--start-date", default="", help="Trade-date range start for daily datasets, YYYYMMDD.")
    parser.add_argument("--end-date", default="", help="Trade-date range end for daily datasets, YYYYMMDD.")
    parser.add_argument("--ann-start-date", default="", help="Announcement-date range start, YYYYMMDD.")
    parser.add_argument("--ann-end-date", default="", help="Announcement-date range end, YYYYMMDD.")
    parser.add_argument("--ts-code", action="append", default=[], help="TS code for ts_code-loop datasets; may repeat.")
    parser.add_argument("--customs-url", action="append", default=[], help="Override gov.cn customs summary URL; may repeat.")
    parser.add_argument("--daily-lookback", type=int, default=DEFAULT_DAILY_LOOKBACK, help="Default open-trading-day lookback when no trade-date range is given.")
    parser.add_argument("--announcement-lookback", type=int, default=DEFAULT_ANNOUNCEMENT_LOOKBACK, help="Default calendar-day lookback when no announcement-date range is given.")
    parser.add_argument("--ops-log-db-path", default="", help="Optional unified source-fetch log SQLite path.")
    return parser.parse_args()


def normalize_date(text: str) -> str:
    value = str(text or "").strip().replace("-", "")
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"invalid date: {text}")
    return value


def daterange(start_text: str, end_text: str) -> List[str]:
    start_day = datetime.strptime(start_text, "%Y%m%d").date()
    end_day = datetime.strptime(end_text, "%Y%m%d").date()
    days: List[str] = []
    current = start_day
    while current <= end_day:
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def expand_dataset_args(raw_values: List[str]) -> List[str]:
    tokens: List[str] = []
    for raw in raw_values:
        for item in str(raw or "").split(","):
            text = item.strip()
            if text:
                tokens.append(text)
    if not tokens or any(item.lower() == "all" for item in tokens):
        return [name for name, spec in DATASET_SPECS.items() if spec.default_enabled]
    names: List[str] = []
    for item in tokens:
        if item not in DATASET_SPECS:
            raise ValueError(f"unknown dataset: {item}")
        if item not in names:
            names.append(item)
    return names


def init_tushare_client() -> TushareClient:
    client = TushareClient(
        {
            "enabled": True,
            "token_env": "TUSHARE_TOKEN",
            "token": os.environ.get("TUSHARE_TOKEN", ""),
            "rate_limit_sleep_seconds": 0.85,
            "max_retry": 3,
            "retry_sleep_seconds": 2.0,
            "rate_limit_backoff_seconds": 12.0,
            "retry_on_rate_limit": True,
        }
    )
    if not client.enabled():
        raise RuntimeError("Tushare client is not enabled. Ensure tushare is installed and TUSHARE_TOKEN is available.")
    return client


def load_trade_dates(client: TushareClient, start_date: str, end_date: str) -> List[str]:
    calendar = client.call("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
    if calendar.empty:
        return []
    frame = calendar.copy()
    frame["is_open"] = pd.to_numeric(frame.get("is_open"), errors="coerce").fillna(0).astype(int)
    dates = frame.loc[frame["is_open"] == 1, "cal_date"].astype(str).tolist()
    return sorted(set(dates))


def latest_calendar_range(days: int) -> tuple[str, str]:
    end_day = date.today()
    start_day = end_day - timedelta(days=max(days - 1, 0))
    return start_day.strftime("%Y%m%d"), end_day.strftime("%Y%m%d")


def latest_trade_range(client: TushareClient, lookback_days: int) -> tuple[str, str]:
    probe_start, probe_end = latest_calendar_range(max(lookback_days * 3, 15))
    dates = load_trade_dates(client, probe_start, probe_end)
    if not dates:
        return probe_start, probe_end
    picked = dates[-max(lookback_days, 1):]
    return picked[0], picked[-1]


def stringify_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if pd.isna(value):
        return ""
    return value


def frame_to_rows(spec: DatasetSpec, frame: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    if frame.empty:
        return rows
    for _, row in frame.iterrows():
        item = {str(key): stringify_value(value) for key, value in row.to_dict().items()}
        record_values = [str(item.get(field, "") or "").strip() for field in spec.key_fields]
        if not all(record_values):
            continue
        record_key = "||".join(record_values)
        if record_key in seen_keys:
            continue
        seen_keys.add(record_key)
        primary_date = next((str(item.get(field, "") or "").strip() for field in spec.primary_date_fields if str(item.get(field, "") or "").strip()), "")
        secondary_date = next((str(item.get(field, "") or "").strip() for field in spec.secondary_date_fields if str(item.get(field, "") or "").strip()), "")
        rows.append(
            {
                "dataset": spec.name,
                "source_name": spec.source_name,
                "record_key": record_key,
                "primary_date": primary_date,
                "secondary_date": secondary_date,
                "ts_code": str(item.get("ts_code", "") or "").strip(),
                "symbol": str(item.get("symbol", "") or "").strip(),
                "name": str(item.get("name", "") or "").strip(),
                "payload_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
        )
    return rows


def write_snapshot(snapshot_root: Path, dataset: str, frame: pd.DataFrame) -> Path | None:
    if frame.empty:
        return None
    snapshot_root.mkdir(parents=True, exist_ok=True)
    path = snapshot_root / f"{dataset}.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def fetch_stock_basic(client: TushareClient) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    fields = "ts_code,symbol,name,area,industry,market,list_date,delist_date,is_hs,list_status"
    for status in ["L", "D", "P"]:
        frame = client.call("stock_basic", exchange="", list_status=status, fields=fields)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"], keep="first")
    return merged.fillna("")


def fetch_loop_by_dates(
    client: TushareClient,
    spec: DatasetSpec,
    dates: List[str],
    *,
    started_at: float,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    total = len(dates)
    for index, current_date in enumerate(dates, start=1):
        log_event(started_at, "dataset_batch_start", dataset=spec.name, batch=index, total_batches=total, date=current_date)
        parameter_name = "trade_date" if spec.mode == "trade_date_loop" else "ann_date"
        frame = client.call(spec.api_name, **{parameter_name: current_date})
        if not frame.empty:
            frames.append(frame.fillna(""))
        log_event(started_at, "dataset_batch_done", dataset=spec.name, batch=index, total_batches=total, date=current_date, rows=len(frame))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates().fillna("")


def fetch_fina_indicator(
    client: TushareClient,
    ts_codes: List[str],
    start_date: str,
    end_date: str,
    *,
    started_at: float,
) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    total = len(ts_codes)
    for index, ts_code in enumerate(ts_codes, start=1):
        log_event(started_at, "dataset_batch_start", dataset="fina_indicator", batch=index, total_batches=total, ts_code=ts_code)
        frame = client.call("fina_indicator", ts_code=ts_code, start_date=start_date, end_date=end_date)
        if not frame.empty:
            frames.append(frame.fillna(""))
        log_event(started_at, "dataset_batch_done", dataset="fina_indicator", batch=index, total_batches=total, ts_code=ts_code, rows=len(frame))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates().fillna("")


def fetch_customs_summary(urls: List[str], *, started_at: float) -> pd.DataFrame:
    _, fetch_one = _ensure_customs_helper()
    records: List[Dict[str, Any]] = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        log_event(started_at, "dataset_batch_start", dataset="customs_summary", batch=index, total_batches=total, url=url)
        record = fetch_one(url)
        payload = dict(record.get("fields", {}) or {})
        payload["source_url"] = record.get("source_url", "")
        payload["status_code"] = record.get("status_code", "")
        payload["title"] = record.get("title", "")
        payload["description"] = record.get("description", "")
        payload["release_date"] = record.get("release_date", "")
        records.append(payload)
        log_event(started_at, "dataset_batch_done", dataset="customs_summary", batch=index, total_batches=total, rows=1, status=record.get("status_code", ""))
    return pd.DataFrame(records).fillna("") if records else pd.DataFrame()


def fetch_html(url: str, *, cookie: str = "") -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20, context=UNVERIFIED_SSL_CONTEXT) as response:
        return response.read().decode("utf-8", errors="ignore")


def _clean_html_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = text.replace("&nbsp;", " ")
    return " ".join(text.split())


def fetch_ccgp_bid_awards(*, started_at: float) -> pd.DataFrame:
    import re

    url = "https://www.ccgp.gov.cn/cggg/zygg/zbgg/"
    log_event(started_at, "dataset_batch_start", dataset="ccgp_bid_awards", url=url)
    html = fetch_html(url)
    pattern = re.compile(
        r'<li>\s*<a href="(?P<href>[^"]+)"[^>]*title="(?P<title>[^"]*)">.*?</a>\s*发布时间：<em>(?P<published_at>[^<]*)</em>\s*地域：<em>(?P<region>[^<]*)</em>\s*采购人：<em>(?P<purchaser>[^<]*)</em>',
        re.S,
    )
    records: List[Dict[str, Any]] = []
    as_of_date = datetime.now().strftime("%Y%m%d")
    for match in pattern.finditer(html):
        href = _clean_html_text(match.group("href"))
        title = _clean_html_text(match.group("title"))
        if not href or not title:
            continue
        records.append(
            {
                "title": title,
                "published_at": _clean_html_text(match.group("published_at")),
                "region": _clean_html_text(match.group("region")),
                "purchaser": _clean_html_text(match.group("purchaser")),
                "source_url": urljoin(url, href),
                "source_site": "ccgp",
                "notice_type": "bid_award",
                "as_of_date": as_of_date,
            }
        )
    log_event(started_at, "dataset_batch_done", dataset="ccgp_bid_awards", rows=len(records), url=url)
    return pd.DataFrame(records).fillna("") if records else pd.DataFrame()


def fetch_ppi_market_digest(*, started_at: float) -> pd.DataFrame:
    import re

    url = "https://www.100ppi.com/"
    log_event(started_at, "dataset_batch_start", dataset="ppi_market_digest", url=url)
    challenge_html = fetch_html(url)
    token_match = re.search(r'var\s+_0x2\s*=\s*"([0-9a-f]+)"', challenge_html)
    cookie = f"HW_CHECK={token_match.group(1)}" if token_match else ""
    html = fetch_html(url, cookie=cookie)
    as_of_date = datetime.now().strftime("%Y%m%d")
    records: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    focus_pattern = re.compile(r'<a href="(?P<href>/focus/\d+\.html)"[^>]*title="(?P<title>[^"]+)"', re.S)
    for match in focus_pattern.finditer(html):
        source_url = urljoin(url, _clean_html_text(match.group("href")))
        if source_url in seen_urls:
            continue
        seen_urls.add(source_url)
        records.append(
            {
                "title": _clean_html_text(match.group("title")),
                "source_url": source_url,
                "source_site": "100ppi",
                "digest_type": "focus",
                "as_of_date": as_of_date,
            }
        )

    forecast_pattern = re.compile(r'<a href="(?P<href>/forecast/detail-[^"]+)"\s+target="_blank"\s+title="(?P<title>[^"]+)"', re.S)
    for match in forecast_pattern.finditer(html):
        source_url = urljoin(url, _clean_html_text(match.group("href")))
        if source_url in seen_urls:
            continue
        seen_urls.add(source_url)
        records.append(
            {
                "title": _clean_html_text(match.group("title")),
                "source_url": source_url,
                "source_site": "100ppi",
                "digest_type": "forecast",
                "as_of_date": as_of_date,
            }
        )

    log_event(started_at, "dataset_batch_done", dataset="ppi_market_digest", rows=len(records), url=url)
    return pd.DataFrame(records).fillna("") if records else pd.DataFrame()


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _row_date_key(payload: Dict[str, Any]) -> Tuple[str, str]:
    primary = str(payload.get("ann_date", "") or payload.get("trade_date", "") or payload.get("end_date", "") or payload.get("release_date", "") or "")
    secondary = str(payload.get("end_date", "") or payload.get("f_ann_date", "") or "")
    return primary, secondary


def _load_affordable_payloads(conn: sqlite3.Connection, dataset: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT payload_json
        FROM affordable_dataset_rows
        WHERE dataset = ?
        """,
        (dataset,),
    ).fetchall()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _latest_by_ts_code(payloads: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for payload in payloads:
        ts_code = str(payload.get("ts_code", "")).strip()
        if not ts_code:
            continue
        key = _row_date_key(payload)
        existing = latest.get(ts_code)
        if existing is None or key > _row_date_key(existing):
            latest[ts_code] = payload
    return latest


def _build_forecast_midpoint(payload: Dict[str, Any]) -> Tuple[float | None, str, float | None]:
    low = _coerce_float(payload.get("net_profit_min"))
    high = _coerce_float(payload.get("net_profit_max"))
    if low is not None and high is not None:
        midpoint = (low + high) / 2.0
        revision = None
        last_parent = _coerce_float(payload.get("last_parent_net"))
        if last_parent not in (None, 0.0):
            revision = (midpoint - last_parent) / abs(last_parent)
        return midpoint, "forecast_net_profit_range", revision
    pct_low = _coerce_float(payload.get("p_change_min"))
    pct_high = _coerce_float(payload.get("p_change_max"))
    last_parent = _coerce_float(payload.get("last_parent_net"))
    if last_parent not in (None, 0.0) and pct_low is not None and pct_high is not None:
        pct_mid = (pct_low + pct_high) / 200.0
        midpoint = last_parent * (1.0 + pct_mid)
        revision = (midpoint - last_parent) / abs(last_parent)
        return midpoint, "forecast_pct_change_proxy", revision
    return None, "", None


def _build_express_profit(payload: Dict[str, Any]) -> Tuple[float | None, str]:
    for field in ("n_income", "total_profit", "operate_profit"):
        value = _coerce_float(payload.get(field))
        if value is not None:
            return value, field
    return None, ""


def _select_growth_proxy(payload: Dict[str, Any]) -> Tuple[float | None, str]:
    for field in ("q_dtprofit_yoy", "q_netprofit_yoy", "netprofit_yoy", "dt_netprofit_yoy", "roe_yoy"):
        value = _coerce_float(payload.get(field))
        if value is not None:
            return value, field
    return None, ""


def _build_model_profit(fina_payload: Dict[str, Any] | None, daily_payload: Dict[str, Any] | None) -> Tuple[float | None, str, float | None]:
    if not daily_payload:
        return None, "", None
    total_mv = _coerce_float(daily_payload.get("total_mv"))
    pe_ttm = _coerce_float(daily_payload.get("pe_ttm") or daily_payload.get("pe"))
    if total_mv is None or pe_ttm is None or pe_ttm <= 0:
        return None, "", None
    implied_trailing_profit = total_mv / pe_ttm
    growth_value = None
    growth_field = ""
    if fina_payload:
        growth_value, growth_field = _select_growth_proxy(fina_payload)
    if growth_value is None:
        return implied_trailing_profit, "market_implied_trailing_profit", None
    clipped = max(-0.8, min(2.0, growth_value / 100.0))
    forward_profit = implied_trailing_profit * (1.0 + clipped * 0.5)
    return forward_profit, f"market_implied_plus_{growth_field}", growth_value / 100.0


def build_internal_expectation(conn: sqlite3.Connection, *, started_at: float) -> pd.DataFrame:
    log_event(started_at, "dataset_batch_start", dataset="internal_expectation", phase="load_payloads")
    stock_basic_latest = _latest_by_ts_code(_load_affordable_payloads(conn, "stock_basic"))
    forecast_latest = _latest_by_ts_code(_load_affordable_payloads(conn, "forecast"))
    express_latest = _latest_by_ts_code(_load_affordable_payloads(conn, "express"))
    fina_latest = _latest_by_ts_code(_load_affordable_payloads(conn, "fina_indicator"))
    daily_basic_latest = _latest_by_ts_code(_load_affordable_payloads(conn, "daily_basic"))
    universe = sorted(set(stock_basic_latest) | set(forecast_latest) | set(express_latest) | set(fina_latest) | set(daily_basic_latest))
    log_event(started_at, "dataset_batch_done", dataset="internal_expectation", phase="load_payloads", universe=len(universe))

    rows: List[Dict[str, Any]] = []
    as_of_date = datetime.now().strftime("%Y%m%d")
    for index, ts_code in enumerate(universe, start=1):
        if index == 1 or index % 500 == 0 or index == len(universe):
            log_event(started_at, "dataset_batch_start", dataset="internal_expectation", phase="score_symbol", batch=index, total_batches=len(universe), ts_code=ts_code)
        stock_payload = stock_basic_latest.get(ts_code, {})
        forecast_payload = forecast_latest.get(ts_code, {})
        express_payload = express_latest.get(ts_code, {})
        fina_payload = fina_latest.get(ts_code, {})
        daily_payload = daily_basic_latest.get(ts_code, {})

        express_profit, express_source = _build_express_profit(express_payload)
        forecast_profit, forecast_source, forecast_revision = _build_forecast_midpoint(forecast_payload)
        model_profit, model_source, model_growth = _build_model_profit(fina_payload, daily_payload)

        expected_profit = None
        source_mix: List[str] = []
        confidence = 0.0
        revision = None

        if express_profit is not None:
            expected_profit = express_profit
            source_mix.append(f"express:{express_source}")
            confidence = 0.95
            if model_profit is not None:
                expected_profit = express_profit * 0.85 + model_profit * 0.15
                source_mix.append(f"model:{model_source}")
        elif forecast_profit is not None:
            expected_profit = forecast_profit
            source_mix.append(f"forecast:{forecast_source}")
            confidence = 0.78
            revision = forecast_revision
            if model_profit is not None:
                expected_profit = forecast_profit * 0.75 + model_profit * 0.25
                source_mix.append(f"model:{model_source}")
        elif model_profit is not None:
            expected_profit = model_profit
            source_mix.append(f"model:{model_source}")
            confidence = 0.42 if model_growth is not None else 0.30
            revision = model_growth
        else:
            continue

        if revision is None:
            last_parent = _coerce_float(forecast_payload.get("last_parent_net"))
            if last_parent not in (None, 0.0) and expected_profit is not None:
                revision = (expected_profit - last_parent) / abs(last_parent)

        growth_proxy, growth_proxy_field = _select_growth_proxy(fina_payload) if fina_payload else (None, "")
        pe_ttm = _coerce_float(daily_payload.get("pe_ttm") or daily_payload.get("pe"))
        valuation_bucket = "unknown"
        if pe_ttm is not None:
            if pe_ttm <= 15:
                valuation_bucket = "low"
            elif pe_ttm <= 35:
                valuation_bucket = "mid"
            else:
                valuation_bucket = "high"

        rows.append(
            {
                "ts_code": ts_code,
                "symbol": str(stock_payload.get("symbol", "") or daily_payload.get("ts_code", "")).replace(".SH", "").replace(".SZ", ""),
                "name": str(stock_payload.get("name", "")).strip(),
                "as_of_date": as_of_date,
                "expected_profit": round(expected_profit, 4) if expected_profit is not None else "",
                "expected_profit_lower": round(_coerce_float(forecast_payload.get("net_profit_min")), 4) if _coerce_float(forecast_payload.get("net_profit_min")) is not None else "",
                "expected_profit_upper": round(_coerce_float(forecast_payload.get("net_profit_max")), 4) if _coerce_float(forecast_payload.get("net_profit_max")) is not None else "",
                "revision_ratio": round(revision, 6) if revision is not None else "",
                "confidence": round(confidence, 4),
                "source_mix": "|".join(source_mix),
                "valuation_bucket": valuation_bucket,
                "growth_proxy": round(growth_proxy, 4) if growth_proxy is not None else "",
                "growth_proxy_field": growth_proxy_field,
                "forecast_ann_date": str(forecast_payload.get("ann_date", "") or ""),
                "express_ann_date": str(express_payload.get("ann_date", "") or ""),
                "fina_ann_date": str(fina_payload.get("ann_date", "") or ""),
                "daily_trade_date": str(daily_payload.get("trade_date", "") or ""),
            }
        )
    return pd.DataFrame(rows).fillna("")


def execute_dataset(
    conn: sqlite3.Connection,
    client: TushareClient | None,
    spec: DatasetSpec,
    args: argparse.Namespace,
    snapshot_root: Path,
    run_id: str,
    started_at: float,
) -> Dict[str, Any]:
    run_started = datetime.now()
    params: Dict[str, Any] = {}
    rows_written = 0
    message = ""
    status = "success"
    frame = pd.DataFrame()
    latest_primary_date = ""
    try:
        if spec.mode == "stock_basic_snapshot":
            if client is None:
                raise RuntimeError("Tushare client unavailable")
            frame = fetch_stock_basic(client)
        elif spec.mode == "trade_date_loop":
            if client is None:
                raise RuntimeError("Tushare client unavailable")
            start_date = normalize_date(args.start_date) if args.start_date else ""
            end_date = normalize_date(args.end_date) if args.end_date else ""
            if not start_date or not end_date:
                start_date, end_date = latest_trade_range(client, args.daily_lookback)
            params.update({"start_date": start_date, "end_date": end_date})
            trade_dates = load_trade_dates(client, start_date, end_date)
            frame = fetch_loop_by_dates(client, spec, trade_dates, started_at=started_at)
        elif spec.mode == "ann_date_loop":
            if client is None:
                raise RuntimeError("Tushare client unavailable")
            ann_start = normalize_date(args.ann_start_date) if args.ann_start_date else ""
            ann_end = normalize_date(args.ann_end_date) if args.ann_end_date else ""
            if not ann_start or not ann_end:
                ann_start, ann_end = latest_calendar_range(args.announcement_lookback)
            params.update({"ann_start_date": ann_start, "ann_end_date": ann_end})
            ann_dates = daterange(ann_start, ann_end)
            frame = fetch_loop_by_dates(client, spec, ann_dates, started_at=started_at)
        elif spec.mode == "ts_code_loop":
            if client is None:
                raise RuntimeError("Tushare client unavailable")
            ts_codes = [str(item).strip() for item in args.ts_code if str(item).strip()]
            if spec.requires_ts_codes and not ts_codes:
                raise RuntimeError(f"{spec.name} requires at least one --ts-code")
            start_date = normalize_date(args.start_date) if args.start_date else ""
            end_date = normalize_date(args.end_date) if args.end_date else ""
            if not start_date or not end_date:
                start_date, end_date = latest_calendar_range(365)
            params.update({"start_date": start_date, "end_date": end_date, "ts_codes": ts_codes})
            frame = fetch_fina_indicator(client, ts_codes, start_date, end_date, started_at=started_at)
        elif spec.mode == "customs_urls":
            default_urls, _ = _ensure_customs_helper()
            urls = [str(item).strip() for item in args.customs_url if str(item).strip()] or default_urls
            params.update({"urls": urls})
            frame = fetch_customs_summary(urls, started_at=started_at)
        elif spec.mode == "internal_expectation":
            frame = build_internal_expectation(conn, started_at=started_at)
        elif spec.mode == "ccgp_bid_awards":
            frame = fetch_ccgp_bid_awards(started_at=started_at)
        elif spec.mode == "ppi_market_digest":
            frame = fetch_ppi_market_digest(started_at=started_at)
        else:
            raise RuntimeError(f"unsupported dataset mode: {spec.mode}")

        rows = frame_to_rows(spec, frame)
        rows_written = upsert_rows(conn, rows)
        if spec.primary_date_fields:
            for field in spec.primary_date_fields:
                if field in frame.columns:
                    series = frame[field].astype(str).str.strip()
                    series = series.loc[series.ne("")]
                    if not series.empty:
                        latest_primary_date = str(series.max())
                        break
        snapshot_path = write_snapshot(snapshot_root, spec.name, frame)
        message = f"snapshot={snapshot_path}" if snapshot_path else "no rows"
        return {
            "dataset": spec.name,
            "source_name": spec.source_name,
            "status": status,
            "rows_written": rows_written,
            "frame_rows": int(len(frame)),
            "latest_primary_date": latest_primary_date,
            "message": message,
            "params": params,
        }
    except Exception as exc:
        status = "failed"
        message = str(exc)
        return {
            "dataset": spec.name,
            "source_name": spec.source_name,
            "status": status,
            "rows_written": 0,
            "frame_rows": int(len(frame)),
            "latest_primary_date": latest_primary_date,
            "message": message,
            "params": params,
        }
    finally:
        write_run_record(
            conn,
            run_id=run_id,
            dataset=spec.name,
            source_name=spec.source_name,
            status=status,
            row_count=rows_written,
            started_at=run_started,
            finished_at=datetime.now(),
            params=params,
            message=message,
        )


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    db_path = Path(args.db_path).resolve()
    snapshot_root = Path(args.snapshot_root).resolve()
    datasets = expand_dataset_args(args.dataset)
    runtime_config = build_runtime_config()
    ops_log_db_path = Path(args.ops_log_db_path).resolve() if str(args.ops_log_db_path or "").strip() else resolve_research_fact_sqlite_path(runtime_config)

    client: TushareClient | None = None
    tushare_datasets = [name for name in datasets if DATASET_SPECS[name].source_name == "tushare"]
    if tushare_datasets:
        client = init_tushare_client()

    conn = sqlite_connection(db_path)
    ensure_schema(conn)
    try:
        with research_fact_sqlite_connection(ops_log_db_path) as ops_conn:
            ensure_research_fact_schema(ops_conn)
            log_event(started_at, "affordable_bundle_start", run_id=run_id, db_path=db_path, dataset_count=len(datasets))
            summary: List[Dict[str, Any]] = []
            for index, dataset_name in enumerate(datasets, start=1):
                spec = DATASET_SPECS[dataset_name]
                log_event(started_at, "dataset_start", index=index, total=len(datasets), dataset=dataset_name, source=spec.source_name)
                dataset_started_at = datetime.now()
                result = execute_dataset(
                    conn=conn,
                    client=client,
                    spec=spec,
                    args=args,
                    snapshot_root=snapshot_root,
                    run_id=run_id,
                    started_at=started_at,
                )
                dataset_finished_at = datetime.now()
                summary.append(result)
                insert_source_fetch_logs(
                    ops_conn,
                    [build_source_fetch_log(run_id=run_id, result=result, started_at=dataset_started_at, finished_at=dataset_finished_at)],
                )
                log_event(
                    started_at,
                    "dataset_done",
                    index=index,
                    total=len(datasets),
                    dataset=dataset_name,
                    status=result["status"],
                    frame_rows=result["frame_rows"],
                    rows_written=result["rows_written"],
                )
            report = {
                "run_id": run_id,
                "db_path": str(db_path),
                "snapshot_root": str(snapshot_root),
                "ops_log_db_path": str(ops_log_db_path),
                "results": summary,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            log_event(started_at, "affordable_bundle_done", run_id=run_id)
            return 0 if all(item["status"] == "success" for item in summary) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
