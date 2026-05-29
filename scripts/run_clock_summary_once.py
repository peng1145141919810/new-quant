from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_manifest(repo_root: Path) -> dict:
    return json.loads((repo_root / "SYSTEM_MANIFEST.yaml").read_text(encoding="utf-8-sig"))


def _runtime_root(repo_root: Path) -> Path:
    manifest = _load_manifest(repo_root)
    canonical = dict(manifest.get("canonical", {}) or {})
    candidates = [
        Path(str(canonical.get("workspace_runtime_root", "") or "")).resolve(),
        Path(str(canonical.get("live_runtime_root", "") or "")).resolve(),
        (repo_root / "src" / "ashare").resolve(),
    ]
    for candidate in candidates:
        if (candidate / "engine").exists():
            return candidate
    return (repo_root / "src" / "ashare").resolve()


REPO_ROOT = _repo_root()
RUNTIME_ROOT = _runtime_root(REPO_ROOT)
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from engine.clock_supervisor import _build_daily_pack, _ensure_cycle_state, _run_audit_site_publish, _run_intraday_state_refresh
from engine.config_utils import load_config
from engine.trading_clock import clock_now


def _scheduler_runtime_path(config: dict) -> Path:
    trade_clock_root = Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve()
    return trade_clock_root / "runtime" / "scheduler_runtime.json"


def _resolve_runtime_selection(repo_root: Path, explicit_config: str, explicit_profile: str) -> tuple[Path, str]:
    if str(explicit_config).strip():
        return Path(explicit_config).resolve(), str(explicit_profile or "").strip()
    bootstrap_config = {
        "paths": {
            "trade_clock_root": str((repo_root / "data" / "trade_clock").resolve()),
        }
    }
    scheduler_runtime = {}
    runtime_path = _scheduler_runtime_path(bootstrap_config)
    if runtime_path.exists():
        try:
            scheduler_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except Exception:
            scheduler_runtime = {}
    config_path = Path(str(scheduler_runtime.get("config_path", "") or "")).resolve() if str(scheduler_runtime.get("config_path", "") or "").strip() else Path()
    profile = str(explicit_profile or scheduler_runtime.get("service_profile", "") or "").strip()
    if not config_path.exists():
        raise SystemExit("No scheduler runtime config_path available for summary phase.")
    return config_path, profile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the trade-clock summary phase once.")
    parser.add_argument("--config", default="", help="Optional explicit runtime config path.")
    parser.add_argument("--profile", default="", help="Optional explicit scheduler profile.")
    parser.add_argument("--trade-date", default="", help="Optional explicit trade date in YYYY-MM-DD.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path, profile = _resolve_runtime_selection(REPO_ROOT, str(args.config).strip(), str(args.profile).strip())
    config = load_config(config_path)
    trade_date = str(args.trade_date or clock_now(str(dict(config.get("trade_clock", {}) or {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai")).date().isoformat())
    cycle_state = _ensure_cycle_state(config, trade_date, profile)
    intraday_refresh = _run_intraday_state_refresh(config=config, trade_date=trade_date, source_phase="summary", cycle_state=cycle_state)
    manifest = _build_daily_pack(config=config, trade_date=trade_date, profile=profile, cycle_state=cycle_state)
    if intraday_refresh.get("ran", False):
        manifest["intraday_state_machine"] = intraday_refresh
    publish_result = _run_audit_site_publish(config=config, trade_date=trade_date, report_dir=Path(str(manifest.get("pack_dir", "") or "")).resolve())
    if publish_result.get("ran", False):
        manifest["audit_site_publish"] = publish_result
    if publish_result.get("ran", False) and not publish_result.get("ok", False) and not publish_result.get("fail_open", True):
        raise SystemExit(str(publish_result.get("message", "") or "audit_site_publish_failed"))
    print(json.dumps({"ok": True, "status": "summary_completed", "manifest": manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
