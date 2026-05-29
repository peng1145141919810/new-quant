# -*- coding: utf-8 -*-
"""V6 数据清单与健康状态。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .config_utils import ensure_dir


def _file_dataset(dataset_name: str, path: Path, keys: List[str], stale_hours: int) -> Dict[str, Any]:
    """根据文件构造数据集状态。"""
    if not path.exists():
        return {
            "dataset_name": dataset_name,
            "path": str(path),
            "grain": "unknown",
            "keys": keys,
            "last_refresh_time": None,
            "expected_refresh_frequency": "daily",
            "freshness_status": "missing",
            "missing_ratio": 1.0,
            "owner_module": "runtime_scanner",
            "recompute_triggers": [],
        }
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    age_hours = (datetime.now() - mtime).total_seconds() / 3600.0
    freshness_status = "fresh" if age_hours <= stale_hours else "stale"
    missing_ratio = 0.0
    try:
        if path.suffix.lower() == ".parquet":
            df = pd.read_parquet(path)
            if len(df) == 0:
                missing_ratio = 1.0
            elif keys:
                present_keys = [k for k in keys if k in df.columns]
                if present_keys:
                    missing_ratio = float(df[present_keys].isna().any(axis=1).mean())
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not payload:
                missing_ratio = 1.0
    except Exception:
        missing_ratio = 0.0
    return {
        "dataset_name": dataset_name,
        "path": str(path),
        "grain": "daily",
        "keys": keys,
        "last_refresh_time": mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "expected_refresh_frequency": "daily",
        "freshness_status": freshness_status,
        "missing_ratio": missing_ratio,
        "owner_module": "runtime_scanner",
        "recompute_triggers": [],
    }


def build_inventory_real(config: Dict[str, Any]) -> Dict[str, Any]:
    """扫描真实文件，构造数据清单。"""
    stale_hours = int(config.get("data_gap_engine", {}).get("stale_hours_hard_refresh", 36) or 36)
    daily_cache_root = Path(str(config["paths"]["daily_cache_root"]))
    event_store_root = Path(str(config["paths"]["event_store_root"]))
    inventory_root = Path(str(config["paths"]["inventory_root"]))
    datasets = [
        _file_dataset("trade_calendar", daily_cache_root / "trade_calendar.parquet", ["cal_date"], stale_hours),
        _file_dataset("stock_basic", daily_cache_root / "stock_basic.parquet", ["ts_code"], stale_hours),
        _file_dataset("daily_latest", daily_cache_root / "daily_latest.parquet", ["ts_code", "trade_date"], stale_hours),
        _file_dataset("daily_basic_latest", daily_cache_root / "daily_basic_latest.parquet", ["ts_code", "trade_date"], stale_hours),
        _file_dataset("adj_factor_latest", daily_cache_root / "adj_factor_latest.parquet", ["ts_code", "trade_date"], stale_hours),
        _file_dataset("event_store", event_store_root / "event_store.jsonl", [], stale_hours),
        _file_dataset("data_gap_report", inventory_root / "data_gap_report.json", [], stale_hours),
    ]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "datasets": datasets,
    }


def save_inventory(config: Dict[str, Any], inventory: Dict[str, Any]) -> Path:
    """保存数据清单。"""
    root = Path(str(config["paths"]["inventory_root"]))
    ensure_dir(root)
    out_path = root / "data_inventory.json"
    out_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
