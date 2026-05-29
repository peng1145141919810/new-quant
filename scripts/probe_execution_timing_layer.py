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


def _now_dt(trade_date: str, time_text: str, timezone: str) -> datetime:
    resolved_date = str(trade_date or datetime.now().date().isoformat())[:10]
    resolved_time = str(time_text or "09:50:00").strip() or "09:50:00"
    return datetime.fromisoformat(f"{resolved_date}T{resolved_time}").replace(tzinfo=ZoneInfo(timezone))


def _available_columns(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in frame.columns]


def _records(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    out = frame.loc[:, columns].copy()
    out = out.where(pd.notna(out), None)
    return out.to_dict("records")


def _json_safe(value):
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def build_probe_config(write_root: Path, read_root: Path) -> dict:
    return {
        "paths": {
            "trade_clock_root": str((write_root / "trade_clock").resolve()),
            "trade_release_root": str((read_root / "trade_release_v1").resolve()),
            "oms_output_root": str((read_root / "live_execution_bridge" / "oms_v1").resolve()),
            "live_execution_root": str((read_root / "live_execution_bridge").resolve()),
            "live_price_snapshot_path": str((read_root / "live_execution_bridge" / "daily_price_snapshot.csv").resolve()),
            "technical_confirmation_root": str((read_root / "event_lake_v6" / "research" / "technical_confirmation").resolve()),
            "affordable_snapshot_root": str((Path(r"H:\Ashare\data") / "affordable_feeds" / "latest").resolve()),
            "automation_runs_root": str((Path(r"H:\Ashare\outputs\automation_runs")).resolve()),
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
            "artifact_root": str((write_root / "trade_clock" / "intraday_state_probe").resolve()),
            "stale_order_minutes": 20,
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


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Lightweight probe for the execution timing layer.")
    ap.add_argument("--trade-date", default="", help="Explicit trade date in YYYY-MM-DD.")
    ap.add_argument("--time", default="09:50:00", help="Probe clock time in HH:MM:SS.")
    ap.add_argument("--source-phase", default="shadow", help="Source phase label.")
    ap.add_argument("--read-root", default="", help="Optional read-only data root override.")
    ap.add_argument("--write-root", default=r"H:\Ashare\data", help="Writable output data root.")
    ap.add_argument("--top", type=int, default=12, help="How many top timing rows to print.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    read_root = Path(str(args.read_root).strip() or _default_read_root()).resolve()
    write_root = Path(str(args.write_root).strip() or r"H:\Ashare\data").resolve()
    config = build_probe_config(write_root=write_root, read_root=read_root)
    trade_date = str(args.trade_date).strip()
    now_dt = _now_dt(trade_date=trade_date or datetime.now().date().isoformat(), time_text=str(args.time), timezone="Asia/Shanghai")
    result = refresh_intraday_state_machine(
        config=config,
        trade_date=trade_date,
        source_phase=str(args.source_phase).strip() or "shadow",
        cycle_state=None,
        now_dt=now_dt,
    )
    manifest = dict(result.get("manifest", {}) or {})
    symbol_path = Path(str(manifest.get("symbol_state_path", "") or ""))
    control_path = Path(str(manifest.get("control_summary_path", "") or ""))
    frame = pd.read_csv(symbol_path) if symbol_path.exists() else pd.DataFrame()
    control = json.loads(control_path.read_text(encoding="utf-8")) if control_path.exists() else {}
    probe_rows = []
    if not frame.empty:
        keep = _available_columns(
            frame,
            [
                "stock_code",
                "symbol_state",
                "timing_state",
                "buy_timing_score",
                "sell_timing_score",
                "t_overlay_state",
                "t_direction",
                "t_allowed_ratio",
                "feature_quality_tier",
                "timing_freeze_reason",
            ],
        )
        sort_keys = [column for column in ["buy_timing_score", "sell_timing_score"] if column in frame.columns]
        ordered = frame.sort_values(sort_keys, ascending=[False] * len(sort_keys)) if sort_keys else frame.copy()
        probe_rows = (
            ordered
            .head(int(args.top))
            .pipe(_records, keep)
        )
    payload = {
        "result": result,
        "control_summary": {
            "timing_window": control.get("timing_window"),
            "projected_afternoon_window": control.get("projected_afternoon_window"),
            "timing_enabled_symbols": control.get("timing_enabled_symbols"),
            "buy_ready_count": control.get("buy_ready_count"),
            "sell_ready_count": control.get("sell_ready_count"),
            "t_eligible_symbols": control.get("t_eligible_symbols"),
            "t_triggered_symbols": control.get("t_triggered_symbols"),
            "feature_quality": control.get("timing_feature_quality"),
        },
        "top_rows": probe_rows,
    }
    print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
