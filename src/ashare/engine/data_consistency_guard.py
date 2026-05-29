# -*- coding: utf-8 -*-
"""Automation-time data freshness and consistency checks."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

from .research_fact_store import resolve_research_fact_sqlite_path
from .sql_store import resolve_sqlite_path


def _safe_date(text: str) -> datetime | None:
    raw = str(text or "").strip()[:19]
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def _fetchone(db_path: Path, sql: str, params: Iterable[Any] = ()) -> tuple | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return row if row is not None else None
    except Exception:
        return None
    finally:
        conn.close()


def _max_trade_date(db_path: Path, table: str, column: str) -> str:
    row = _fetchone(db_path, f"SELECT MAX({column}) FROM {table}")
    return str(row[0] or "").strip() if row else ""


def _latest_pipeline_finish(db_path: Path, pipeline_name: str, trade_date: str) -> Dict[str, Any]:
    row = _fetchone(
        db_path,
        """
        SELECT
            MAX(COALESCE(finished_at, started_at, '')),
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),
            COUNT(*),
            MAX(COALESCE(publish_date, ''))
        FROM source_fetch_run_log
        WHERE pipeline_name = ?
          AND trade_date = ?
        """,
        (pipeline_name, trade_date),
    )
    if not row:
        return {"pipeline_name": pipeline_name, "trade_date": trade_date, "finished_at": "", "success_count": 0, "row_count": 0, "latest_publish_date": ""}
    return {
        "pipeline_name": pipeline_name,
        "trade_date": trade_date,
        "finished_at": str(row[0] or ""),
        "success_count": int(row[1] or 0),
        "row_count": int(row[2] or 0),
        "latest_publish_date": str(row[3] or ""),
    }


def assess_automation_data_readiness(
    config: Dict[str, Any],
    *,
    trade_date: str,
    phase_name: str,
) -> Dict[str, Any]:
    scheduler_cfg = dict(dict(config.get("trade_clock", {}) or {}).get("scheduler", {}) or {})
    gate_cfg = dict(scheduler_cfg.get("data_consistency_gate", {}) or {})
    if not bool(gate_cfg.get("enabled", True)):
        return {"enabled": False, "ok": True, "phase_name": phase_name, "trade_date": trade_date}

    fact_db = resolve_research_fact_sqlite_path(config)
    research_db = resolve_sqlite_path(config)
    max_market_age_days = max(int(gate_cfg.get("max_market_age_days", 4) or 4), 1)
    max_market_table_spread_days = max(int(gate_cfg.get("max_market_table_spread_days", 1) or 1), 0)
    required_today_phases = {
        str(item or "").strip()
        for item in list(
            gate_cfg.get(
                "require_today_refresh_phases",
                ["research_refresh", "release_refresh", "preopen_gate", "simulation", "midday_review", "afternoon_execution", "summary"],
            )
            or []
        )
        if str(item or "").strip()
    }
    required_today_pipelines = [
        str(item or "").strip()
        for item in list(
            gate_cfg.get(
                "required_today_pipelines",
                ["affordable_data_refresh", "external_research_refresh", "research_fact_refresh", "industry_hard_factor_refresh"],
            )
            or []
        )
        if str(item or "").strip()
    ]

    issues: list[str] = []
    market_dates = {
        "market_enriched_daily": _max_trade_date(research_db, "market_enriched_daily", "trade_date"),
        "market_hs300_daily": _max_trade_date(research_db, "market_hs300_daily", "trade_date"),
        "market_price_snapshot": _max_trade_date(research_db, "market_price_snapshot", "trade_date"),
    }
    daily_market_keys = ("market_enriched_daily", "market_hs300_daily")
    daily_market_dts = [_safe_date(market_dates.get(key, "")) for key in daily_market_keys if market_dates.get(key, "")]
    snapshot_dt = _safe_date(market_dates.get("market_price_snapshot", ""))
    market_dts = [item for item in [*daily_market_dts, snapshot_dt] if item is not None]
    if not market_dts:
        issues.append("market_dates_missing")
        market_anchor_days = 999
        market_spread_days = 999
    else:
        anchor_dt = max(item for item in market_dts if item is not None)
        target_dt = _safe_date(trade_date) or datetime.now()
        market_anchor_days = max((target_dt.date() - anchor_dt.date()).days, 0)
        if daily_market_dts:
            market_spread_days = (
                max(item for item in daily_market_dts if item is not None).date()
                - min(item for item in daily_market_dts if item is not None).date()
            ).days
        else:
            market_spread_days = 0
        if market_anchor_days > max_market_age_days:
            issues.append(f"market_data_too_old:{market_anchor_days}d")
        if market_spread_days > max_market_table_spread_days:
            issues.append(f"market_table_mismatch:{market_spread_days}d")

    today_refresh = {}
    night_window_covered = True
    if phase_name in required_today_phases:
        for pipeline_name in required_today_pipelines:
            state = _latest_pipeline_finish(fact_db, pipeline_name, trade_date)
            today_refresh[pipeline_name] = state
            ok = state["success_count"] > 0
            if not ok:
                issues.append(f"missing_today_refresh:{pipeline_name}")
                night_window_covered = False

    return {
        "enabled": True,
        "ok": len(issues) == 0,
        "phase_name": str(phase_name or ""),
        "trade_date": str(trade_date or ""),
        "issues": issues,
        "night_window_covered": bool(night_window_covered),
        "market_dates": market_dates,
        "market_anchor_days": int(market_anchor_days),
        "market_table_spread_days": int(market_spread_days),
        "today_refresh": today_refresh,
        "fact_sqlite_path": str(fact_db),
        "research_sqlite_path": str(research_db),
    }
