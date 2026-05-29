# -*- coding: utf-8 -*-
"""Tushare 轻量客户端。"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, Optional

import pandas as pd

try:
    import tushare as ts
except Exception:
    ts = None


class TushareClient:
    """带重试和限流的 Tushare 客户端。"""

    def __init__(self, cfg: Dict[str, Any]):
        """初始化客户端。

        Args:
            cfg: tushare provider 配置。

        Returns:
            None
        """
        self.cfg = dict(cfg or {})
        token_env = str(self.cfg.get("token_env", "TUSHARE_TOKEN") or "TUSHARE_TOKEN")
        self.token = str(self.cfg.get("token") or os.environ.get(token_env, "") or "").strip()
        self.sleep_seconds = float(self.cfg.get("rate_limit_sleep_seconds", 0.8) or 0.8)
        self.max_retry = int(self.cfg.get("max_retry", 3) or 3)
        self.retry_sleep_seconds = float(self.cfg.get("retry_sleep_seconds", 2.0) or 2.0)
        self.rate_limit_backoff_seconds = float(self.cfg.get("rate_limit_backoff_seconds", 12.0) or 12.0)
        self.retry_on_rate_limit = bool(self.cfg.get("retry_on_rate_limit", False))
        self.last_call_at = 0.0
        self.last_error = ""
        self.raw_ts = ts
        self.pro = ts.pro_api(self.token) if (self.token and ts is not None) else None

    def enabled(self) -> bool:
        """判断是否可用。

        Args:
            None

        Returns:
            bool: 是否可用。
        """
        return bool(self.cfg.get("enabled", True)) and self.pro is not None

    def _respect_rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_call_at
        if elapsed < self.sleep_seconds:
            time.sleep(self.sleep_seconds - elapsed)

    def _mark_call(self) -> None:
        self.last_call_at = time.monotonic()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        hints = [
            "rate limit",
            "频次",
            "调用太频繁",
            "每分钟最多访问",
            "请稍后再试",
            "429",
            "too many requests",
        ]
        return any(item in text for item in hints)

    def call(self, api_name: str, **kwargs: Any) -> pd.DataFrame:
        """调用 Tushare 接口。

        Args:
            api_name: 接口名。
            **kwargs: 接口参数。

        Returns:
            pd.DataFrame: 返回表；失败时返回空表。
        """
        if not self.enabled():
            return pd.DataFrame()
        fn = getattr(self.pro, api_name, None)
        if fn is None:
            return pd.DataFrame()
        last_exc: Optional[Exception] = None
        for i in range(self.max_retry):
            try:
                self._respect_rate_limit()
                df = fn(**kwargs)
                self._mark_call()
                self.last_error = ""
                return df if df is not None else pd.DataFrame()
            except Exception as exc:
                last_exc = exc
                self.last_error = f"{api_name}: {exc}"
                self._mark_call()
                if self._is_rate_limit_error(exc) and not self.retry_on_rate_limit:
                    break
                sleep_seconds = self.retry_sleep_seconds * (i + 1)
                if self._is_rate_limit_error(exc):
                    sleep_seconds = max(sleep_seconds, self.rate_limit_backoff_seconds * (i + 1))
                time.sleep(sleep_seconds)
        if last_exc:
            return pd.DataFrame()
        return pd.DataFrame()

    def realtime_quote(
        self,
        *,
        ts_codes: Iterable[str] | None = None,
        src: str = "sina",
    ) -> pd.DataFrame:
        """Fetch realtime quote snapshots from the public crawler endpoint."""
        if not bool(self.cfg.get("enabled", True)) or self.raw_ts is None or not self.token:
            return pd.DataFrame()
        fn = getattr(self.raw_ts, "realtime_quote", None)
        if fn is None:
            return pd.DataFrame()
        code_items = [str(item or "").strip().upper() for item in list(ts_codes or []) if str(item or "").strip()]
        kwargs: Dict[str, Any] = {"src": str(src or "sina").strip() or "sina"}
        if code_items:
            kwargs["ts_code"] = ",".join(code_items)
        last_exc: Optional[Exception] = None
        for i in range(self.max_retry):
            try:
                self._respect_rate_limit()
                self.raw_ts.set_token(self.token)
                df = fn(**kwargs)
                self._mark_call()
                self.last_error = ""
                return df if df is not None else pd.DataFrame()
            except Exception as exc:
                last_exc = exc
                self.last_error = f"realtime_quote: {exc}"
                self._mark_call()
                sleep_seconds = self.retry_sleep_seconds * (i + 1)
                if self._is_rate_limit_error(exc):
                    sleep_seconds = max(sleep_seconds, self.rate_limit_backoff_seconds * (i + 1))
                time.sleep(sleep_seconds)
        if last_exc:
            return pd.DataFrame()
        return pd.DataFrame()

    def realtime_tick(
        self,
        *,
        ts_code: str,
        src: str = "sina",
    ) -> pd.DataFrame:
        """Fetch realtime tick history for a single symbol from the public crawler endpoint."""
        if not bool(self.cfg.get("enabled", True)) or self.raw_ts is None or not self.token:
            return pd.DataFrame()
        fn = getattr(self.raw_ts, "realtime_tick", None)
        if fn is None:
            return pd.DataFrame()
        normalized = str(ts_code or "").strip().upper()
        if not normalized:
            return pd.DataFrame()
        kwargs: Dict[str, Any] = {"ts_code": normalized, "src": str(src or "sina").strip() or "sina"}
        last_exc: Optional[Exception] = None
        for i in range(self.max_retry):
            try:
                self._respect_rate_limit()
                self.raw_ts.set_token(self.token)
                df = fn(**kwargs)
                self._mark_call()
                self.last_error = ""
                return df if df is not None else pd.DataFrame()
            except Exception as exc:
                last_exc = exc
                self.last_error = f"realtime_tick: {exc}"
                self._mark_call()
                sleep_seconds = self.retry_sleep_seconds * (i + 1)
                if self._is_rate_limit_error(exc):
                    sleep_seconds = max(sleep_seconds, self.rate_limit_backoff_seconds * (i + 1))
                time.sleep(sleep_seconds)
        if last_exc:
            return pd.DataFrame()
        return pd.DataFrame()

    def realtime_list(
        self,
        *,
        src: str = "dc",
    ) -> pd.DataFrame:
        """Fetch realtime ranking/list snapshot from the public crawler endpoint."""
        if not bool(self.cfg.get("enabled", True)) or self.raw_ts is None or not self.token:
            return pd.DataFrame()
        fn = getattr(self.raw_ts, "realtime_list", None)
        if fn is None:
            return pd.DataFrame()
        kwargs: Dict[str, Any] = {"src": str(src or "dc").strip() or "dc"}
        last_exc: Optional[Exception] = None
        for i in range(self.max_retry):
            try:
                self._respect_rate_limit()
                self.raw_ts.set_token(self.token)
                df = fn(**kwargs)
                self._mark_call()
                self.last_error = ""
                return df if df is not None else pd.DataFrame()
            except Exception as exc:
                last_exc = exc
                self.last_error = f"realtime_list: {exc}"
                self._mark_call()
                sleep_seconds = self.retry_sleep_seconds * (i + 1)
                if self._is_rate_limit_error(exc):
                    sleep_seconds = max(sleep_seconds, self.rate_limit_backoff_seconds * (i + 1))
                time.sleep(sleep_seconds)
        if last_exc:
            return pd.DataFrame()
        return pd.DataFrame()

    def rt_min(
        self,
        *,
        ts_codes: Iterable[str] | None = None,
        freq: str = "1MIN",
    ) -> pd.DataFrame:
        """Fetch official realtime minute bars for one or more symbols."""
        if not self.enabled():
            return pd.DataFrame()
        code_items = [str(item or "").strip().upper() for item in list(ts_codes or []) if str(item or "").strip()]
        if not code_items:
            return pd.DataFrame()
        kwargs: Dict[str, Any] = {
            "ts_code": ",".join(code_items),
            "freq": str(freq or "1MIN").strip().upper() or "1MIN",
        }
        return self.call("rt_min", **kwargs)
