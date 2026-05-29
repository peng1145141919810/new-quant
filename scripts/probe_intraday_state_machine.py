from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
    if local_contract:
        return local
    return legacy


def build_probe_config(write_root: Path, read_root: Path) -> dict:
    return {
        "paths": {
            "trade_clock_root": str((read_root / "trade_clock").resolve()),
            "trade_release_root": str((read_root / "trade_release_v1").resolve()),
            "oms_output_root": str((read_root / "live_execution_bridge" / "oms_v1").resolve()),
            "live_execution_root": str((read_root / "live_execution_bridge").resolve()),
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
            "fail_open": False,
            "artifact_root": str((write_root / "trade_clock" / "intraday_state").resolve()),
            "stale_order_minutes": 20,
        },
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Lightweight shadow probe for the intraday state machine.")
    ap.add_argument("--trade-date", default="", help="Optional explicit trade date in YYYY-MM-DD.")
    ap.add_argument("--source-phase", default="probe", help="Optional source phase label.")
    ap.add_argument("--read-root", default="", help="Optional read-only data root override.")
    ap.add_argument("--write-root", default=r"H:\Ashare\data", help="Writable output data root.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    read_root = Path(str(args.read_root).strip() or _default_read_root()).resolve()
    write_root = Path(str(args.write_root).strip() or r"H:\Ashare\data").resolve()
    config = build_probe_config(write_root=write_root, read_root=read_root)
    result = refresh_intraday_state_machine(
        config=config,
        trade_date=str(args.trade_date).strip(),
        source_phase=str(args.source_phase).strip() or "probe",
        cycle_state=None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
