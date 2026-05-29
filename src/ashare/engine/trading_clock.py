from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd

from .tushare_client import TushareClient

ASIA_SHANGHAI = "Asia/Shanghai"
CALENDAR_COLUMNS = ["exchange", "cal_date", "is_open", "pretrade_date"]


@dataclass(frozen=True)
class ExecutionWindow:
    label: str
    start: time
    end: time


def _safe_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame(columns=CALENDAR_COLUMNS)
    out = df.copy()
    for column in CALENDAR_COLUMNS:
        if column not in out.columns:
            out[column] = ""
    out = out[CALENDAR_COLUMNS].copy()
    out["cal_date"] = out["cal_date"].astype(str).str.strip()
    out["pretrade_date"] = out["pretrade_date"].astype(str).str.strip()
    out["is_open"] = pd.to_numeric(out["is_open"], errors="coerce").fillna(0).astype(int)
    out = out.loc[out["cal_date"].ne("")].drop_duplicates(subset=["cal_date"], keep="last")
    return out.sort_values("cal_date").reset_index(drop=True)


def _clock_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("trade_clock", {}) or {})


def _calendar_path(config: Dict[str, Any]) -> Path:
    return Path(str(config.get("paths", {}).get("trading_calendar_cache_path", "") or "")).resolve()


def _parse_hms(text: str, default: str) -> time:
    raw = str(text or default).strip() or default
    return datetime.strptime(raw, "%H:%M:%S").time()


def clock_now(timezone_name: str = ASIA_SHANGHAI) -> datetime:
    return datetime.now(ZoneInfo(str(timezone_name or ASIA_SHANGHAI)))


def market_stage(now: datetime) -> str:
    current = now.timetz().replace(tzinfo=None)
    if current < time(9, 15):
        return "pre_open"
    if current < time(9, 25):
        return "opening_auction"
    if current < time(9, 30):
        return "pre_open_pause"
    if current < time(11, 30):
        return "morning_session"
    if current < time(13, 0):
        return "midday_break"
    if current < time(14, 57):
        return "afternoon_session"
    if current < time(15, 0):
        return "closing_auction"
    return "post_close"


def load_execution_windows(config: Dict[str, Any]) -> List[ExecutionWindow]:
    cfg = _clock_cfg(config)
    raw_windows = list(cfg.get("execution_windows", []) or [])
    if not raw_windows:
        raw_windows = [
            {"label": "morning_primary", "start": "09:30:30", "end": "10:00:00"},
        ]
    windows: List[ExecutionWindow] = []
    for idx, item in enumerate(raw_windows, start=1):
        label = str(item.get("label", "") or f"window_{idx}").strip() or f"window_{idx}"
        start = _parse_hms(str(item.get("start", "") or "09:30:30"), "09:30:30")
        end = _parse_hms(str(item.get("end", "") or "10:00:00"), "10:00:00")
        windows.append(ExecutionWindow(label=label, start=start, end=end))
    return sorted(windows, key=lambda item: (item.start, item.end, item.label))


def current_execution_window(config: Dict[str, Any], now: datetime | None = None) -> ExecutionWindow | None:
    current_dt = now or clock_now(str(_clock_cfg(config).get("timezone", ASIA_SHANGHAI) or ASIA_SHANGHAI))
    current = current_dt.timetz().replace(tzinfo=None)
    for window in load_execution_windows(config):
        if window.start <= current <= window.end:
            return window
    return None


def _load_calendar_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=CALENDAR_COLUMNS)
    try:
        return _safe_frame(pd.read_csv(path))
    except Exception:
        return pd.DataFrame(columns=CALENDAR_COLUMNS)


def _save_calendar_cache(path: Path, df: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_frame(df).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _fetch_calendar_frame(config: Dict[str, Any], start_date: date, end_date: date) -> pd.DataFrame:
    client = TushareClient(dict(config.get("providers", {}).get("tushare", {}) or {}))
    if not client.enabled():
        return pd.DataFrame(columns=CALENDAR_COLUMNS)
    df = client.call(
        "trade_cal",
        exchange="",
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    return _safe_frame(df)


def ensure_trading_calendar(
    config: Dict[str, Any],
    around_date: date | None = None,
    lookback_days: int | None = None,
    forward_days: int | None = None,
) -> Dict[str, Any]:
    target_date = around_date or clock_now(str(_clock_cfg(config).get("timezone", ASIA_SHANGHAI) or ASIA_SHANGHAI)).date()
    release_cfg = dict(config.get("trade_release", {}) or {})
    lookback = int(lookback_days if lookback_days is not None else release_cfg.get("calendar_lookback_days", 7) or 7)
    forward = int(forward_days if forward_days is not None else release_cfg.get("calendar_forward_days", 45) or 45)
    start_date = target_date - timedelta(days=lookback)
    end_date = target_date + timedelta(days=forward)
    cache_path = _calendar_path(config)
    cached = _load_calendar_cache(cache_path)
    expected_dates = {d.strftime("%Y%m%d") for d in pd.date_range(start=start_date, end=end_date, freq="D")}
    known_dates = set(cached.get("cal_date", pd.Series(dtype=str)).astype(str).tolist()) if not cached.empty else set()
    missing_dates = sorted(expected_dates - known_dates)
    fetched = pd.DataFrame(columns=CALENDAR_COLUMNS)
    if missing_dates:
        fetched = _fetch_calendar_frame(config=config, start_date=start_date, end_date=end_date)
        if not fetched.empty:
            cached = _safe_frame(pd.concat([cached, fetched], ignore_index=True))
            _save_calendar_cache(cache_path, cached)
    return {
        "cache_path": str(cache_path),
        "calendar": cached,
        "missing_dates": missing_dates,
        "fetched": int(len(fetched.index)) if not fetched.empty else 0,
        "ok": bool(not cached.empty),
    }


def _row_for_date(calendar_df: pd.DataFrame, target_date: date) -> pd.Series | None:
    if calendar_df.empty:
        return None
    key = target_date.strftime("%Y%m%d")
    rows = calendar_df.loc[calendar_df["cal_date"].astype(str).eq(key)]
    if rows.empty:
        return None
    return rows.iloc[-1]


def is_trading_day(config: Dict[str, Any], target_date: date | None = None) -> Dict[str, Any]:
    current_date = target_date or clock_now(str(_clock_cfg(config).get("timezone", ASIA_SHANGHAI) or ASIA_SHANGHAI)).date()
    calendar_info = ensure_trading_calendar(config=config, around_date=current_date)
    row = _row_for_date(calendar_info["calendar"], current_date)
    if row is None:
        return {
            "ok": False,
            "date": current_date.isoformat(),
            "is_trading_day": False,
            "reason": "calendar_missing",
            "calendar_path": calendar_info["cache_path"],
        }
    return {
        "ok": True,
        "date": current_date.isoformat(),
        "is_trading_day": bool(int(row.get("is_open", 0) or 0) == 1),
        "pretrade_date": str(row.get("pretrade_date", "") or ""),
        "calendar_path": calendar_info["cache_path"],
    }


def next_trading_day(config: Dict[str, Any], base_date: date, include_today: bool = False) -> Dict[str, Any]:
    calendar_info = ensure_trading_calendar(config=config, around_date=base_date)
    calendar_df = calendar_info["calendar"]
    if calendar_df.empty:
        return {
            "ok": False,
            "base_date": base_date.isoformat(),
            "next_trading_day": "",
            "reason": "calendar_empty",
            "calendar_path": calendar_info["cache_path"],
        }
    base_key = base_date.strftime("%Y%m%d")
    candidates = calendar_df.loc[calendar_df["is_open"].astype(int).eq(1)].copy()
    if not include_today:
        candidates = candidates.loc[candidates["cal_date"].astype(str) > base_key]
    else:
        candidates = candidates.loc[candidates["cal_date"].astype(str) >= base_key]
    if candidates.empty:
        return {
            "ok": False,
            "base_date": base_date.isoformat(),
            "next_trading_day": "",
            "reason": "no_open_day_found",
            "calendar_path": calendar_info["cache_path"],
        }
    row = candidates.sort_values("cal_date").iloc[0]
    cal_date = str(row.get("cal_date", "") or "")
    return {
        "ok": True,
        "base_date": base_date.isoformat(),
        "next_trading_day": f"{cal_date[:4]}-{cal_date[4:6]}-{cal_date[6:8]}",
        "calendar_path": calendar_info["cache_path"],
    }


def trading_clock_snapshot(config: Dict[str, Any], now: datetime | None = None) -> Dict[str, Any]:
    current_dt = now or clock_now(str(_clock_cfg(config).get("timezone", ASIA_SHANGHAI) or ASIA_SHANGHAI))
    trading_day_info = is_trading_day(config=config, target_date=current_dt.date())
    window = current_execution_window(config=config, now=current_dt)
    return {
        "now": current_dt.isoformat(timespec="seconds"),
        "market_stage": market_stage(current_dt),
        "is_trading_day": bool(trading_day_info.get("is_trading_day", False)),
        "calendar_ok": bool(trading_day_info.get("ok", False)),
        "calendar_path": str(trading_day_info.get("calendar_path", "") or ""),
        "active_execution_window": {
            "label": window.label,
            "start": window.start.strftime("%H:%M:%S"),
            "end": window.end.strftime("%H:%M:%S"),
        } if window else None,
    }
