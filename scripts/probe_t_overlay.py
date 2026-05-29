from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src/ashare" / "src/ashare"


PACKAGE_ROOT = _package_root()
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from engine.intraday_state_machine.runtime import refresh_intraday_state_machine


def _default_read_root() -> Path:
    local = Path(r"H:\Ashare\data")
    legacy = Path(r"F:\quant_data\Ashare\data")
    local_contract = (
        (local / "trade_clock" / "system_safety_state.json").exists()
        and (local / "trade_release_v1" / "latest_release.json").exists()
        and (local / "live_execution_bridge" / "oms_v1" / "snapshots" / "oms_summary.json").exists()
    )
    return local if local_contract else legacy


def _build_config(write_root: Path, read_root: Path) -> dict:
    return {
        "paths": {
            "trade_clock_root": str((write_root / "trade_clock").resolve()),
            "trade_release_root": str((read_root / "trade_release_v1").resolve()),
            "oms_output_root": str((read_root / "live_execution_bridge" / "oms_v1").resolve()),
            "live_execution_root": str((read_root / "live_execution_bridge").resolve()),
            "live_price_snapshot_path": str((read_root / "live_execution_bridge" / "daily_price_snapshot.csv").resolve()),
            "technical_confirmation_root": str((read_root / "event_lake_v6" / "research" / "technical_confirmation").resolve()),
            "affordable_snapshot_root": str((Path(r"H:\Ashare\data") / "affordable_feeds" / "latest").resolve()),
        },
        "trade_clock": {
            "timezone": "Asia/Shanghai",
            "scheduler": {
                "simulation_namespace": "simulation",
                "shadow_namespace": "shadow",
            },
        },
        "oms": {
            "output_root": str((read_root / "live_execution_bridge" / "oms_v1").resolve()),
        },
        "intraday_state_machine": {
            "enabled": True,
            "shadow_mode": True,
            "fail_open": False,
            "artifact_root": str((write_root / "trade_clock" / "intraday_t_probe").resolve()),
            "timing_layer": {
                "enabled": True,
                "buy_score_threshold": 0.58,
                "sell_score_threshold": 0.62,
                "require_oms_clean_state": True,
                "require_flow_confirmation": True,
                "enable_afternoon_second_leg": True,
            },
            "t_overlay": {
                "enabled": True,
                "max_rounds_per_symbol_per_day": 1,
                "max_ratio_per_symbol": 0.20,
                "disable_on_panic": True,
                "disable_on_major_event": True,
            },
        },
    }


def _probe_dt(trade_date: str, time_text: str) -> datetime:
    date_text = str(trade_date or datetime.now().date().isoformat())[:10]
    return datetime.fromisoformat(f"{date_text}T{time_text}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def _series_from(frame: pd.DataFrame, column: str, default: object = "") -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame.index), index=frame.index)


def _available_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


def _json_safe(value):
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _run_once(config: dict, trade_date: str, phase: str, now_dt: datetime) -> dict:
    result = refresh_intraday_state_machine(
        config=config,
        trade_date=trade_date,
        source_phase=phase,
        cycle_state=None,
        now_dt=now_dt,
    )
    manifest = dict(result.get("manifest", {}) or {})
    symbol_path = Path(str(manifest.get("symbol_state_path", "") or ""))
    frame = pd.read_csv(symbol_path) if symbol_path.exists() else pd.DataFrame()
    if frame.empty:
        rows: list[dict] = []
    else:
        subset = frame.loc[
            _series_from(frame, "t_eligible", False).fillna(False).astype(bool)
            | _series_from(frame, "t_triggered", False).fillna(False).astype(bool)
            | _series_from(frame, "t_overlay_state", "").astype(str).ne("t_disabled")
        ].copy()
        if subset.empty:
            subset = frame.head(8).copy()
        keep = _available_columns(
            subset,
            [
                "stock_code",
                "symbol_state",
                "timing_state",
                "buy_timing_score",
                "sell_timing_score",
                "t_overlay_state",
                "t_direction",
                "t_leg_done",
                "t_allowed_ratio",
                "t_triggered",
                "t_trigger_reason",
            ],
        )
        payload = subset.loc[:, keep].copy().where(pd.notna(subset.loc[:, keep]), None)
        rows = payload.to_dict("records")
    return {
        "phase": phase,
        "probe_time": now_dt.isoformat(),
        "result": result,
        "rows": rows,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Probe T overlay first-leg and second-leg states.")
    ap.add_argument("--trade-date", default="", help="Explicit trade date in YYYY-MM-DD.")
    ap.add_argument("--read-root", default="", help="Optional read-only data root override.")
    ap.add_argument("--write-root", default=r"H:\Ashare\data", help="Writable output data root.")
    ap.add_argument("--morning-time", default="10:00:00")
    ap.add_argument("--afternoon-time", default="13:20:00")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    read_root = Path(str(args.read_root).strip() or _default_read_root()).resolve()
    write_root = Path(str(args.write_root).strip() or r"H:\Ashare\data").resolve()
    trade_date = str(args.trade_date).strip()
    config = _build_config(write_root=write_root, read_root=read_root)
    morning = _run_once(config=config, trade_date=trade_date, phase="shadow", now_dt=_probe_dt(trade_date, str(args.morning_time)))
    afternoon = _run_once(config=config, trade_date=trade_date, phase="afternoon_shadow", now_dt=_probe_dt(trade_date, str(args.afternoon_time)))
    print(json.dumps(_json_safe({"morning": morning, "afternoon": afternoon}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
