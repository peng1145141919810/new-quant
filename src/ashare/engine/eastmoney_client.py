# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        code, suffix = text.split(".", 1)
        return f"{code.zfill(6)}.{suffix}"
    return text.zfill(6)


def _secid(ts_code: str) -> str:
    symbol = _normalize_symbol(ts_code)
    if not symbol or "." not in symbol:
        return ""
    code, suffix = symbol.split(".", 1)
    market = "1" if suffix == "SH" else "0"
    return f"{market}.{code}"


def _klt_from_freq(freq: str) -> str:
    mapping = {
        "1MIN": "1",
        "5MIN": "5",
        "15MIN": "15",
        "30MIN": "30",
        "60MIN": "60",
        "D": "101",
    }
    return mapping.get(str(freq or "1MIN").strip().upper(), "1")


class EastmoneyClient:
    """Lightweight Eastmoney public market-data client for minute-bar pulls."""

    def __init__(self, cfg: Dict[str, Any] | None = None):
        self.cfg = dict(cfg or {})
        self.enabled_flag = bool(self.cfg.get("enabled", True))
        self.base_url = str(
            self.cfg.get("intraday_kline_base_url", "https://push2his.eastmoney.com/api/qt/stock/kline/get")
            or "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        ).strip()
        self.timeout_seconds = float(self.cfg.get("timeout_seconds", 8.0) or 8.0)
        self.sleep_seconds = float(self.cfg.get("sleep_seconds", 0.15) or 0.15)
        self.max_retry = int(self.cfg.get("max_retry", 2) or 2)
        self.last_error = ""
        self.last_call_at = 0.0

    def enabled(self) -> bool:
        return self.enabled_flag

    def _respect_rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_call_at
        if elapsed < self.sleep_seconds:
            time.sleep(self.sleep_seconds - elapsed)

    def _mark_call(self) -> None:
        self.last_call_at = time.monotonic()

    def _request_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = urlencode(params)
        req = Request(
            f"{self.base_url}?{query}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urlopen(req, timeout=self.timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        return json.loads(raw)

    def intraday_bars(
        self,
        *,
        ts_codes: Iterable[str] | None = None,
        freq: str = "1MIN",
        trade_date: str = "",
    ) -> pd.DataFrame:
        if not self.enabled():
            return pd.DataFrame()
        symbols = [item for item in (_normalize_symbol(x) for x in list(ts_codes or [])) if item]
        if not symbols:
            return pd.DataFrame()
        target_date = str(trade_date or "").strip()[:10]
        beg = target_date.replace("-", "") if target_date else "0"
        end = target_date.replace("-", "") if target_date else "20500101"
        rows: List[Dict[str, Any]] = []
        for ts_code in symbols:
            secid = _secid(ts_code)
            if not secid:
                continue
            params = {
                "secid": secid,
                "klt": _klt_from_freq(freq),
                "fqt": "1",
                "beg": beg,
                "end": end,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            }
            last_exc: Exception | None = None
            payload: Dict[str, Any] = {}
            for idx in range(self.max_retry):
                try:
                    self._respect_rate_limit()
                    payload = self._request_json(params)
                    self._mark_call()
                    self.last_error = ""
                    break
                except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                    last_exc = exc
                    self.last_error = f"intraday_bars:{ts_code}:{exc}"
                    self._mark_call()
                    time.sleep(self.sleep_seconds * (idx + 1))
            if last_exc is not None and not payload:
                continue
            klines = list(dict(payload.get("data", {}) or {}).get("klines", []) or [])
            for item in klines:
                parts = str(item or "").split(",")
                if len(parts) < 7:
                    continue
                dt_text = parts[0].strip()
                try:
                    dt = datetime.strptime(dt_text, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if target_date and dt.strftime("%Y-%m-%d") != target_date:
                    continue
                rows.append(
                    {
                        "ts_code": ts_code,
                        "symbol": ts_code.split(".", 1)[0],
                        "time": dt.strftime("%H:%M:%S"),
                        "bar_datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": parts[1],
                        "close": parts[2],
                        "high": parts[3],
                        "low": parts[4],
                        "vol": parts[5],
                        "amount": parts[6],
                        "amplitude_pct": parts[7] if len(parts) > 7 else "",
                    }
                )
        return pd.DataFrame(rows)
