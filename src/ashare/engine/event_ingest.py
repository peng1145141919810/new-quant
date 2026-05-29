# -*- coding: utf-8 -*-
"""事件接入层：基础数据 + 公告 + 新闻。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .announcement_fetchers import (
    enrich_announcement_text,
    fetch_cninfo_announcements,
    fetch_sse_latest_announcements,
    fetch_szse_latest_announcements,
)
from .config_utils import ensure_dir
from .logging_utils import log_line
from .tushare_client import TushareClient


def _event_root(config: Dict[str, Any]) -> Path:
    """返回原始事件根目录。

    Args:
        config: 配置。

    Returns:
        Path: 根目录。
    """
    return Path(str(config["paths"]["raw_event_root"]))


def _daily_cache_root(config: Dict[str, Any]) -> Path:
    """返回日缓存目录。

    Args:
        config: 配置。

    Returns:
        Path: 目录。
    """
    return Path(str(config["paths"]["daily_cache_root"]))


def _now_str() -> str:
    """返回当前时间字符串。

    Args:
        None

    Returns:
        str: 时间字符串。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tushare_fetch_state_path(config: Dict[str, Any]) -> Path:
    return ensure_dir(_daily_cache_root(config)) / "tushare_fetch_state.json"


def _load_tushare_fetch_state(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _tushare_fetch_state_path(config)
    if not path.exists():
        return {"api_calls": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"api_calls": {}}
    if not isinstance(payload, dict):
        return {"api_calls": {}}
    payload.setdefault("api_calls", {})
    return payload


def _save_tushare_fetch_state(config: Dict[str, Any], state: Dict[str, Any]) -> None:
    path = _tushare_fetch_state_path(config)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _prune_call_history(raw_items: List[str], now_dt: datetime, window_seconds: int) -> List[str]:
    out: List[str] = []
    for item in list(raw_items or []):
        try:
            ts = datetime.fromisoformat(str(item))
        except Exception:
            continue
        if (now_dt - ts).total_seconds() < float(window_seconds):
            out.append(ts.isoformat())
    return out


def _rate_limit_guard(
    state: Dict[str, Any],
    api_name: str,
    now_dt: datetime,
    window_seconds: int,
    max_calls: int,
) -> Tuple[bool, float, str]:
    api_calls = dict(state.get("api_calls", {}) or {})
    history = _prune_call_history(list(api_calls.get(api_name, []) or []), now_dt=now_dt, window_seconds=window_seconds)
    api_calls[api_name] = history
    state["api_calls"] = api_calls
    if len(history) < int(max_calls):
        return True, 0.0, ""
    oldest = datetime.fromisoformat(history[0])
    next_dt = oldest + timedelta(seconds=int(window_seconds))
    wait_seconds = max((next_dt - now_dt).total_seconds(), 0.0)
    return False, wait_seconds, next_dt.strftime("%Y-%m-%d %H:%M:%S")


def _record_api_call(state: Dict[str, Any], api_name: str, now_dt: datetime) -> None:
    api_calls = dict(state.get("api_calls", {}) or {})
    history = list(api_calls.get(api_name, []) or [])
    history.append(now_dt.isoformat())
    api_calls[api_name] = history
    state["api_calls"] = api_calls


def _dedup_raw_items(raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按来源、时间、标题去重。

    Args:
        raw_items: 原始事件。

    Returns:
        List[Dict[str, Any]]: 去重后列表。
    """
    seen = set()
    out = []
    for item in raw_items:
        key = (
            str(item.get("source_name", "")),
            str(item.get("publish_time", ""))[:16],
            str(item.get("title", "")).strip(),
            str(item.get("security_code_hint", "") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _save_raw_events(config: Dict[str, Any], raw_items: List[Dict[str, Any]]) -> Path:
    """保存原始事件 JSONL。

    Args:
        config: 配置。
        raw_items: 原始事件。

    Returns:
        Path: 输出路径。
    """
    root = ensure_dir(_event_root(config) / datetime.now().strftime("%Y%m%d"))
    out_path = root / "raw_events.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        for item in raw_items:
            payload = dict(item)
            key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            payload["raw_id"] = hashlib.md5(key.encode("utf-8")).hexdigest()
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return out_path


def refresh_market_basics(config: Dict[str, Any]) -> Dict[str, Path]:
    """刷新 Tushare 基础表。

    Args:
        config: 配置。

    Returns:
        Dict[str, Path]: 输出路径字典。
    """
    client = TushareClient(config.get("providers", {}).get("tushare", {}))
    root = ensure_dir(_daily_cache_root(config))
    outputs: Dict[str, Path] = {}
    if not client.enabled():
        return outputs
    today = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    trade_cal = client.call("trade_cal", exchange="SSE", start_date=start_date, end_date=today)
    if not trade_cal.empty:
        path = root / "trade_calendar.parquet"
        trade_cal.to_parquet(path, index=False)
        outputs["trade_calendar"] = path
    stock_basic = client.call(
        "stock_basic",
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,area,industry,market,list_date",
    )
    if not stock_basic.empty:
        path = root / "stock_basic.parquet"
        stock_basic.to_parquet(path, index=False)
        outputs["stock_basic"] = path
    latest_open_date = today
    if not trade_cal.empty and "cal_date" in trade_cal.columns:
        open_df = trade_cal.copy()
        if "is_open" in open_df.columns:
            open_df = open_df[open_df["is_open"].astype(str) == "1"]
        if len(open_df) > 0:
            latest_open_date = str(open_df["cal_date"].max())
    for api_name, file_name in [
        ("daily", "daily_latest.parquet"),
        ("daily_basic", "daily_basic_latest.parquet"),
        ("adj_factor", "adj_factor_latest.parquet"),
    ]:
        df = client.call(api_name, trade_date=latest_open_date)
        if not df.empty:
            path = root / file_name
            df.to_parquet(path, index=False)
            outputs[api_name] = path
    log_line(config, f"基础表刷新完成：{list(outputs.keys())}")
    return outputs


def _fetch_tushare_news(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """抓取 Tushare 新闻和长篇新闻。

    Args:
        config: 配置。

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    client = TushareClient(config.get("providers", {}).get("tushare", {}))
    if not client.enabled():
        return [], {"short_news": {}, "major_news": {}, "quota_state": "client_disabled"}
    lookback_hours = int(config.get("event_ingest", {}).get("lookback_hours", 24) or 24)
    ingest_cfg = dict(config.get("event_ingest", {}) or {})
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(hours=lookback_hours)
    start_ts = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_ts = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    rows: List[Dict[str, Any]] = []
    detail: Dict[str, Any] = {"short_news": {}, "major_news": {}}
    state = _load_tushare_fetch_state(config)
    news_sources = list(ingest_cfg.get("news_sources", ["sina", "cls", "wallstreetcn"]) or [])
    major_news_sources = list(ingest_cfg.get("major_news_sources", ["新浪财经", "华尔街见闻", "财联社"]) or [])
    news_source_cap = int(ingest_cfg.get("max_tushare_news_sources_per_run", 1) or 1)
    major_news_source_cap = int(ingest_cfg.get("max_tushare_major_news_sources_per_run", 3) or 3)
    news_window_seconds = int(ingest_cfg.get("tushare_news_rate_window_seconds", 60) or 60)
    news_max_calls = int(ingest_cfg.get("tushare_news_rate_max_calls", 1) or 1)
    major_news_window_seconds = int(ingest_cfg.get("tushare_major_news_rate_window_seconds", 3600) or 3600)
    major_news_max_calls = int(ingest_cfg.get("tushare_major_news_rate_max_calls", 4) or 4)

    if bool(ingest_cfg.get("enable_tushare_news", True)):
        used_sources = 0
        for src in news_sources:
            if used_sources >= news_source_cap:
                detail["short_news"][src] = {"status": "skipped_source_cap"}
                continue
            call_dt = datetime.now()
            allowed, wait_seconds, next_at = _rate_limit_guard(
                state=state,
                api_name="news",
                now_dt=call_dt,
                window_seconds=news_window_seconds,
                max_calls=news_max_calls,
            )
            if not allowed:
                detail["short_news"][src] = {
                    "status": "skipped_local_quota",
                    "wait_seconds": round(wait_seconds, 1),
                    "next_available_at": next_at,
                }
                break
            df = client.call("news", src=src, start_date=start_ts, end_date=end_ts)
            _record_api_call(state=state, api_name="news", now_dt=call_dt)
            used_sources += 1
            if client.last_error:
                detail["short_news"][src] = {"status": "api_error", "error": client.last_error}
                if "最多访问" in client.last_error or "rate limit" in client.last_error.lower():
                    break
                continue
            if df.empty:
                detail["short_news"][src] = {"status": "ok", "rows": 0}
                continue
            detail["short_news"][src] = {"status": "ok", "rows": int(len(df))}
            for _, row in df.iterrows():
                rows.append(
                    {
                        "source_type": "news",
                        "source_name": f"tushare_news_{src}",
                        "publish_time": str(row.get("datetime", "") or row.get("pub_time", "") or ""),
                        "crawl_time": _now_str(),
                        "title": str(row.get("title", "") or row.get("content", "")[:80] or ""),
                        "content": str(row.get("content", "") or ""),
                        "url": str(row.get("url", "") or ""),
                        "security_code_hint": None,
                        "company_name_hint": None,
                    }
                )
    if bool(ingest_cfg.get("enable_tushare_major_news", True)):
        used_sources = 0
        for src in major_news_sources:
            if used_sources >= major_news_source_cap:
                detail["major_news"][src] = {"status": "skipped_source_cap"}
                continue
            call_dt = datetime.now()
            allowed, wait_seconds, next_at = _rate_limit_guard(
                state=state,
                api_name="major_news",
                now_dt=call_dt,
                window_seconds=major_news_window_seconds,
                max_calls=major_news_max_calls,
            )
            if not allowed:
                detail["major_news"][src] = {
                    "status": "skipped_local_quota",
                    "wait_seconds": round(wait_seconds, 1),
                    "next_available_at": next_at,
                }
                break
            df = client.call("major_news", src=src, start_date=start_ts, end_date=end_ts)
            _record_api_call(state=state, api_name="major_news", now_dt=call_dt)
            used_sources += 1
            if client.last_error:
                detail["major_news"][src] = {"status": "api_error", "error": client.last_error}
                if "最多访问" in client.last_error or "rate limit" in client.last_error.lower():
                    break
                continue
            if df.empty:
                detail["major_news"][src] = {"status": "ok", "rows": 0}
                continue
            detail["major_news"][src] = {"status": "ok", "rows": int(len(df))}
            for _, row in df.iterrows():
                rows.append(
                    {
                        "source_type": "news",
                        "source_name": f"tushare_major_news_{src}",
                        "publish_time": str(row.get("pub_time", "") or row.get("datetime", "") or ""),
                        "crawl_time": _now_str(),
                        "title": str(row.get("title", "") or ""),
                        "content": str(row.get("content", "") or row.get("summary", "") or ""),
                        "url": str(row.get("url", "") or ""),
                        "security_code_hint": None,
                        "company_name_hint": None,
                    }
                )
    _save_tushare_fetch_state(config=config, state=state)
    return rows, detail


def _fetch_free_announcements(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """抓取免费公告源。

    Args:
        config: 配置。

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    lookback_hours = int(config.get("event_ingest", {}).get("lookback_hours", 24) or 24)
    ingest_cfg = config.get("event_ingest", {})
    rows: List[Dict[str, Any]] = []
    if bool(ingest_cfg.get("enable_cninfo", True)):
        cninfo_rows = fetch_cninfo_announcements(
            lookback_hours=lookback_hours,
            max_pages_per_market=int(ingest_cfg.get("max_cninfo_pages_per_market", 6) or 6),
        )
        rows.extend(cninfo_rows)
        log_line(config, f"公告子源 cninfo 抓取完成，条数={len(cninfo_rows)}")
    if bool(ingest_cfg.get("enable_sse", True)):
        sse_rows = fetch_sse_latest_announcements()
        rows.extend(sse_rows)
        log_line(config, f"公告子源 sse 抓取完成，条数={len(sse_rows)}")
    if bool(ingest_cfg.get("enable_szse", True)):
        szse_rows = fetch_szse_latest_announcements()
        rows.extend(szse_rows)
        log_line(config, f"公告子源 szse 抓取完成，条数={len(szse_rows)}")
    rows = _dedup_raw_items(rows)
    rows = enrich_announcement_text(
        raw_items=rows,
        pdf_root=_event_root(config) / "pdf_cache",
        max_pdf_fetch_per_run=int(ingest_cfg.get("max_pdf_fetch_per_run", 25) or 25),
        high_value_title_keywords=list(ingest_cfg.get("high_value_title_keywords", [])),
        download_high_value_pdf=bool(ingest_cfg.get("download_pdf_for_high_value_announcements", True)),
    )
    return rows


def ingest_events_real(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """抓取真实事件。

    Args:
        config: 配置。

    Returns:
        List[Dict[str, Any]]: 原始事件列表。
    """
    enabled_sources = set(config.get("event_ingest", {}).get("enabled_sources", []))
    raw_items: List[Dict[str, Any]] = []
    if "announcements" in enabled_sources:
        ann_rows = _fetch_free_announcements(config)
        raw_items.extend(ann_rows)
        log_line(config, f"免费公告抓取完成，条数={len(ann_rows)}")
    if "news" in enabled_sources:
        news_rows, news_detail = _fetch_tushare_news(config)
        raw_items.extend(news_rows)
        log_line(config, f"Tushare 新闻抓取完成，条数={len(news_rows)}，明细={json.dumps(news_detail, ensure_ascii=False)}")
    raw_items = _dedup_raw_items(raw_items)
    _save_raw_events(config, raw_items)
    log_line(config, f"原始事件总条数={len(raw_items)}")
    return raw_items
