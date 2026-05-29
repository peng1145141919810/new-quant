from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from tools.preflight_check import run_preflight
from tools.register_run import finalize_registered_run, start_registered_run


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _load_json_yaml(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _system_manifest(repo_root: Path) -> Dict[str, Any]:
    return _load_json_yaml(repo_root / "SYSTEM_MANIFEST.yaml")


def _run_profiles(repo_root: Path) -> Dict[str, Any]:
    return _load_json_yaml(repo_root / "RUN_PROFILES.yaml")


def _research_python(repo_root: Path, manifest: Dict[str, Any]) -> str:
    canonical = dict(manifest.get("canonical", {}) or {})
    runtime_root = Path(str(canonical.get("workspace_runtime_root") or canonical.get("live_runtime_root")))
    sys.path.insert(0, str(runtime_root))
    from engine import local_settings as LS  # imported lazily to avoid affecting the wrapper startup path

    return str(LS.PYTHON_EXECUTABLE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical governance launcher for the A-share runtime")
    parser.add_argument("--mode", default="", help="Run mode defined in RUN_PROFILES.yaml")
    parser.add_argument("--profile", default="", help="Run profile defined in RUN_PROFILES.yaml")
    parser.add_argument("--config", default="", help="Optional explicit runtime config passed through to main_research_runner.py")
    parser.add_argument("--resume-execution", action="store_true", help="Only applies to resume_downstream")
    parser.add_argument("--release-id", default="", help="Only applies to execution_only")
    parser.add_argument("--ignore-window", action="store_true", help="Only applies to execution_only")
    parser.add_argument("--gate-only", action="store_true", help="Only applies to execution_only")
    parser.add_argument("--execution-mode", default="", choices=["", "simulation", "precision"], help="Execution account mode override")
    parser.add_argument("--precision-trade", default="default", choices=["default", "on", "off"], help="Precision-trade switch override")
    parser.add_argument("--execution-namespace", default="", help="Execution namespace override for simulation/shadow isolation")
    parser.add_argument(
        "--ignore-market-panic-reduce-only",
        default="default",
        choices=["default", "on", "off"],
        help="Execution-only override; when on, PANIC market regime will not force reduce_only.",
    )
    parser.add_argument(
        "--allow-unfinished-orders-reconcile",
        default="default",
        choices=["default", "on", "off"],
        help="Execution-only override; when on, unfinished orders do not hard-block OMS carry/reconcile execution.",
    )
    parser.add_argument("--shadow-run", action="store_true", help="Execution-only shadow-run; keep OMS/audit but do not dispatch broker actions")
    parser.add_argument("--source-summary-path", default="", help="Release-only explicit portfolio_recommendation.json path")
    parser.add_argument("--source-target-positions-path", default="", help="Release-only explicit target_positions.csv path")
    parser.add_argument("--release-note", default="", help="Release-only note written into the release manifest")
    parser.add_argument("--release-source-mode", default="", help="Release-only source_mode override")
    parser.add_argument("--release-trade-date", default="", help="Release-only explicit trade_date override in YYYY-MM-DD")
    parser.add_argument("--tactical-phase", default="", help="intraday_tactics_only phase label")
    parser.add_argument("--tactical-no-execute", action="store_true", help="intraday_tactics_only: generate artifacts only")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip lightweight preflight checks")
    parser.add_argument("--preflight-only", action="store_true", help="Run only the lightweight preflight checks")
    return parser.parse_args()


def _validate_selection(mode: str, profile: str, profiles_doc: Dict[str, Any]) -> None:
    allowed_profiles = dict(profiles_doc.get("allowed_profiles", {}) or {})
    allowed_modes = list(profiles_doc.get("allowed_modes", []) or [])
    if profile not in allowed_profiles:
        raise SystemExit(f"Unsupported profile: {profile}")
    if mode not in allowed_modes:
        raise SystemExit(f"Unsupported mode: {mode}")
    if mode != "resume_downstream":
        return


def _effective_profile(args: argparse.Namespace, profiles_doc: Dict[str, Any]) -> str:
    if str(args.profile).strip():
        return str(args.profile).strip()
    return str(profiles_doc.get("default_profile", "quick_test"))


def _effective_mode(args: argparse.Namespace, profiles_doc: Dict[str, Any], profile: str) -> str:
    if str(args.mode).strip():
        return str(args.mode).strip()
    profile_cfg = dict(profiles_doc.get("allowed_profiles", {}).get(profile, {}) or {})
    return str(profile_cfg.get("mode_default", "integrated_supervisor"))


def _build_command(research_python: str, manifest: Dict[str, Any], args: argparse.Namespace, mode: str, profile: str) -> list[str]:
    main_path = Path(str(manifest["canonical"]["wrapped_business_root_entry"]))
    command = [research_python, str(main_path), "--mode", mode, "--profile", profile]
    if str(args.config).strip():
        command.extend(["--config", str(Path(args.config).resolve())])
    if args.resume_execution:
        command.append("--resume-execution")
    if str(args.release_id).strip():
        command.extend(["--release-id", str(args.release_id).strip()])
    if args.ignore_window:
        command.append("--ignore-window")
    if args.gate_only:
        command.append("--gate-only")
    if str(args.execution_mode).strip():
        command.extend(["--execution-mode", str(args.execution_mode).strip()])
    if str(args.precision_trade).strip().lower() != "default":
        command.extend(["--precision-trade", str(args.precision_trade).strip().lower()])
    if str(args.execution_namespace).strip():
        command.extend(["--execution-namespace", str(args.execution_namespace).strip()])
    if str(args.ignore_market_panic_reduce_only).strip().lower() != "default":
        command.extend(
            [
                "--ignore-market-panic-reduce-only",
                str(args.ignore_market_panic_reduce_only).strip().lower(),
            ]
        )
    if str(args.allow_unfinished_orders_reconcile).strip().lower() != "default":
        command.extend(
            [
                "--allow-unfinished-orders-reconcile",
                str(args.allow_unfinished_orders_reconcile).strip().lower(),
            ]
        )
    if bool(args.shadow_run):
        command.append("--shadow-run")
    if str(args.source_summary_path).strip():
        command.extend(["--source-summary-path", str(Path(args.source_summary_path).resolve())])
    if str(args.source_target_positions_path).strip():
        command.extend(["--source-target-positions-path", str(Path(args.source_target_positions_path).resolve())])
    if str(args.release_note).strip():
        command.extend(["--release-note", str(args.release_note).strip()])
    if str(args.release_source_mode).strip():
        command.extend(["--release-source-mode", str(args.release_source_mode).strip()])
    if str(args.release_trade_date).strip():
        command.extend(["--release-trade-date", str(args.release_trade_date).strip()])
    if str(args.tactical_phase).strip():
        command.extend(["--tactical-phase", str(args.tactical_phase).strip()])
    if bool(args.tactical_no_execute):
        command.append("--tactical-no-execute")
    return command


def main() -> None:
    repo_root = _repo_root()
    manifest = _system_manifest(repo_root)
    profiles_doc = _run_profiles(repo_root)
    args = parse_args()
    profile = _effective_profile(args, profiles_doc)
    mode = _effective_mode(args, profiles_doc, profile)
    _validate_selection(mode=mode, profile=profile, profiles_doc=profiles_doc)

    if bool(args.preflight_only) and bool(args.skip_preflight):
        raise SystemExit("Cannot combine --preflight-only with --skip-preflight.")

    if not args.skip_preflight:
        report = run_preflight(
            repo_root=repo_root,
            profile=profile,
            mode=mode,
            explicit_config=str(args.config).strip(),
        )
        if not bool(report.get("ok", False)):
            raise SystemExit("Preflight failed. See tools/preflight_check.py output for details.")
        if args.preflight_only:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

    research_python = _research_python(repo_root=repo_root, manifest=manifest)
    command = _build_command(research_python=research_python, manifest=manifest, args=args, mode=mode, profile=profile)
    run_payload = start_registered_run(
        repo_root=repo_root,
        mode=mode,
        profile=profile,
        explicit_config=str(args.config).strip(),
        include_resume_execution=bool(args.resume_execution),
        invocation_python=sys.executable,
        research_python=research_python,
    )
    print("===== CANONICAL LAUNCH START =====")
    print("Formal operator entry:", Path(__file__).resolve())
    print("Wrapped business root:", manifest["canonical"]["wrapped_business_root_entry"])
    print("Workspace runtime root:", manifest["canonical"].get("workspace_runtime_root", manifest["canonical"]["live_runtime_root"]))
    print("Mode:", mode)
    print("Profile:", profile)
    print("Research Python:", research_python)
    print("Run ID:", run_payload["run_id"])
    print("Run manifest:", run_payload["run_manifest_path"])
    try:
        subprocess.run(command, cwd=str(repo_root), check=True)
    except BaseException as exc:
        exit_code = getattr(exc, "returncode", None)
        finalize_registered_run(run_payload, status="failed", exit_code=exit_code if isinstance(exit_code, int) else None)
        raise
    finalize_registered_run(run_payload, status="completed", exit_code=0)
    print("===== CANONICAL LAUNCH DONE =====")


if __name__ == "__main__":
    main()
