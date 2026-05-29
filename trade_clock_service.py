from __future__ import annotations

import argparse
import json
import os
import time
import sys
from pathlib import Path
from typing import Any, Dict

from tools.preflight_check import run_preflight


def _package_root() -> Path:
    repo_root = Path(__file__).resolve().parent
    manifest_path = repo_root / "SYSTEM_MANIFEST.yaml"
    candidates: list[Path] = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            manifest = {}
        canonical = dict(manifest.get("canonical", {}) or {})
        for key in ("workspace_runtime_root", "live_runtime_root"):
            raw = str(canonical.get(key, "") or "").strip()
            if raw:
                candidates.append(Path(raw))
    candidates.extend(
        [
            repo_root / "src" / "ashare",
        ]
    )
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate / "engine").exists():
            return candidate
    return (repo_root / "src" / "ashare").resolve()


PACKAGE_ROOT = _package_root()
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from engine import local_settings as LS
from engine.config_builder import build_runtime_config
from engine.clock_supervisor import RuntimeReloadRequested, run_trade_clock
from engine.runtime_profiles import normalize_profile, profile_overrides


def _deep_update(config: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    for section, values in overrides.items():
        bucket = dict(config.get(section, {}) or {})
        bucket.update(values)
        config[section] = bucket
    return config


def _apply_execution_runtime_overrides(
    config: Dict[str, Any],
    execution_mode: str = "",
    precision_trade: str = "default",
    execution_namespace: str = "",
    shadow_run: bool = False,
) -> Dict[str, Any]:
    policy = dict(config.get("execution_policy", {}) or {})
    if str(execution_mode).strip():
        policy["account_mode"] = str(execution_mode).strip().lower()
    if str(precision_trade).strip().lower() == "on":
        policy["precision_trade_enabled"] = True
    elif str(precision_trade).strip().lower() == "off":
        policy["precision_trade_enabled"] = False
    if str(execution_namespace).strip():
        policy["namespace"] = str(execution_namespace).strip()
    policy["shadow_run"] = bool(shadow_run)
    config["execution_policy"] = policy
    return config


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
    return path


def _preflight_status_path(repo_root: Path) -> Path:
    return repo_root / "data" / "trade_clock" / "runtime" / "preflight_status.json"


def _write_preflight_status(repo_root: Path, payload: Dict[str, Any]) -> Path:
    path = _preflight_status_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _write_runtime_config(
    profile: str,
    execution_mode: str = "",
    precision_trade: str = "default",
    execution_namespace: str = "",
    shadow_run: bool = False,
) -> Path:
    resolved_profile = normalize_profile(profile)
    config = build_runtime_config()
    config = _deep_update(config, profile_overrides(resolved_profile))
    config = _apply_execution_runtime_overrides(
        config=config,
        execution_mode=execution_mode,
        precision_trade=precision_trade,
        execution_namespace=execution_namespace,
        shadow_run=shadow_run,
    )
    config["runtime_selection"] = {
        "profile": str(resolved_profile),
        "default_mode": str(getattr(LS, "RUN_MODE", "integrated_supervisor") or "integrated_supervisor"),
        "execution_mode": str(config.get("execution_policy", {}).get("account_mode", "") or ""),
        "precision_trade_enabled": bool(config.get("execution_policy", {}).get("precision_trade_enabled", False)),
        "execution_namespace": str(config.get("execution_policy", {}).get("namespace", "") or ""),
        "shadow_run": bool(config.get("execution_policy", {}).get("shadow_run", False)),
    }
    config_path = PACKAGE_ROOT / "configs" / f"hub_config.v6.runtime.{resolved_profile}.json"
    _atomic_write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lightweight always-on trade clock supervisor")
    parser.add_argument("--profile", default="daily_production", help="Runtime profile used to resolve config")
    parser.add_argument("--config", default="", help="Optional explicit runtime config path")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Optional override for clock poll seconds")
    parser.add_argument("--once", action="store_true", help="Run one heartbeat and exit")
    parser.add_argument("--execution-mode", default="", choices=["", "simulation", "precision"], help="Execution account mode override")
    parser.add_argument("--precision-trade", default="default", choices=["default", "on", "off"], help="Precision-trade switch override")
    parser.add_argument("--execution-namespace", default="", help="Execution namespace override")
    parser.add_argument("--shadow-run", action="store_true", help="Shadow-run execution namespace; keep OMS/audit but do not dispatch broker actions")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip lightweight preflight")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent
    retry_sleep_seconds = max(int(args.poll_seconds or 60), 30)
    while True:
        config_path = (
            Path(args.config).resolve()
            if str(args.config).strip()
            else _write_runtime_config(
                str(args.profile).strip(),
                execution_mode=str(args.execution_mode).strip(),
                precision_trade=str(args.precision_trade).strip(),
                execution_namespace=str(args.execution_namespace).strip(),
                shadow_run=bool(args.shadow_run),
            )
        )
        if not args.skip_preflight:
            phase_reports = []
            for mode_name in ("research_only", "release_only", "execution_only", "midday_review_only"):
                phase_reports.append(
                    run_preflight(
                        repo_root=repo_root,
                        profile=str(args.profile).strip(),
                        mode=mode_name,
                        explicit_config=str(config_path),
                    )
                )
            report = {
                "ok": all(bool(item.get("ok", False)) for item in phase_reports),
                "service": "trade_clock_service",
                "profile": str(args.profile).strip(),
                "checks": phase_reports,
            }
            report["status_path"] = str(_write_preflight_status(repo_root, report))
            if not bool(report.get("ok", False)):
                if bool(args.once):
                    raise SystemExit("Trade clock preflight failed.")
                time.sleep(retry_sleep_seconds)
                continue
        try:
            run_trade_clock(
                config_path=config_path,
                profile=str(args.profile).strip(),
                poll_seconds=(int(args.poll_seconds) if int(args.poll_seconds or 0) > 0 else None),
                once=bool(args.once),
            )
            return
        except RuntimeReloadRequested:
            if bool(args.once):
                raise
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])


if __name__ == "__main__":
    main()
