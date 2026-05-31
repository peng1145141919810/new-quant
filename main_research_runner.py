from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


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

from ashare_control.control_plane import runtime_config_alias_paths, runtime_stage_preview, write_control_plane_snapshot
from engine import local_settings as LS
from engine.config_builder import build_runtime_config
from engine.runtime_profiles import normalize_profile, profile_overrides


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A-share runtime entry")
    parser.add_argument(
        "--mode",
        default=str(LS.RUN_MODE or "integrated_supervisor"),
        choices=[
            "integrated_supervisor",
            "research_only",
            "release_only",
            "execution_only",
            "midday_review_only",
            "resume_downstream",
            "oms_validate",
            "full_cycle",
            "ingest_only",
            "extract_only",
            "gap_only",
            "industry_router_only",
            "plan_only",
            "bridge_only",
            "intraday_tactics_only",
            "evidence_audit_only",
        ],
        help="Runtime mode.",
    )
    parser.add_argument(
        "--profile",
        default=str(LS.DEFAULT_RUN_PROFILE or "overnight"),
        choices=["overnight", "daily_production", "quick_test"],
        help="Runtime profile.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional explicit config path. If omitted, a runtime config is generated from the selected profile.",
    )
    parser.add_argument(
        "--resume-execution",
        action="store_true",
        help="Only applies to resume_downstream. Continue execution after recovering downstream artifacts.",
    )
    parser.add_argument("--release-id", default="", help="Only applies to execution_only.")
    parser.add_argument("--ignore-window", action="store_true", help="Only applies to execution_only.")
    parser.add_argument("--gate-only", action="store_true", help="Only applies to execution_only.")
    parser.add_argument("--execution-mode", default="", choices=["", "simulation", "precision"], help="Execution account mode override.")
    parser.add_argument(
        "--precision-trade",
        default="default",
        choices=["default", "on", "off"],
        help="Precision-trade override.",
    )
    parser.add_argument("--execution-namespace", default="", help="Execution namespace override.")
    parser.add_argument(
        "--ignore-market-panic-reduce-only",
        default="default",
        choices=["default", "on", "off"],
        help="Execution-only override.",
    )
    parser.add_argument(
        "--allow-unfinished-orders-reconcile",
        default="default",
        choices=["default", "on", "off"],
        help="Execution-only override.",
    )
    parser.add_argument("--shadow-run", action="store_true", help="Execution-only shadow run.")
    parser.add_argument("--source-summary-path", default="", help="Release-only explicit portfolio_recommendation.json path.")
    parser.add_argument("--source-target-positions-path", default="", help="Release-only explicit target_positions.csv path.")
    parser.add_argument("--release-note", default="", help="Release-only note written into the release manifest.")
    parser.add_argument("--release-source-mode", default="", help="Release-only source mode override.")
    parser.add_argument("--release-trade-date", default="", help="Release-only explicit trade date in YYYY-MM-DD.")
    parser.add_argument("--tactical-phase", default="", help="intraday_tactics_only phase label.")
    parser.add_argument("--tactical-no-execute", action="store_true", help="intraday_tactics_only artifact-only run.")
    parser.add_argument("--candidate-pool-path", default="", help="Evidence-audit-only explicit candidate_pool.csv path.")
    parser.add_argument("--evidence-limit", type=int, default=0, help="Evidence-audit-only max symbols to audit.")
    return parser.parse_args()


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
    ignore_market_panic_reduce_only: str = "default",
    allow_unfinished_orders_reconcile: str = "default",
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
    if str(ignore_market_panic_reduce_only).strip().lower() == "on":
        policy["ignore_market_panic_reduce_only"] = True
    elif str(ignore_market_panic_reduce_only).strip().lower() == "off":
        policy["ignore_market_panic_reduce_only"] = False
    if str(allow_unfinished_orders_reconcile).strip().lower() == "on":
        policy["allow_unfinished_orders_reconcile"] = True
    elif str(allow_unfinished_orders_reconcile).strip().lower() == "off":
        policy["allow_unfinished_orders_reconcile"] = False
    policy["shadow_run"] = bool(shadow_run)
    config["execution_policy"] = policy
    return config


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
    return path


def _write_runtime_config(
    profile: str,
    execution_mode: str = "",
    precision_trade: str = "default",
    execution_namespace: str = "",
    ignore_market_panic_reduce_only: str = "default",
    allow_unfinished_orders_reconcile: str = "default",
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
        ignore_market_panic_reduce_only=ignore_market_panic_reduce_only,
        allow_unfinished_orders_reconcile=allow_unfinished_orders_reconcile,
        shadow_run=shadow_run,
    )
    config["runtime_selection"] = {
        "profile": str(resolved_profile),
        "default_mode": str(getattr(LS, "RUN_MODE", "integrated_supervisor") or "integrated_supervisor"),
        "execution_mode": str(config.get("execution_policy", {}).get("account_mode", "") or ""),
        "precision_trade_enabled": bool(config.get("execution_policy", {}).get("precision_trade_enabled", False)),
        "execution_namespace": str(config.get("execution_policy", {}).get("namespace", "") or ""),
        "ignore_market_panic_reduce_only": bool(config.get("execution_policy", {}).get("ignore_market_panic_reduce_only", False)),
        "allow_unfinished_orders_reconcile": bool(config.get("execution_policy", {}).get("allow_unfinished_orders_reconcile", False)),
        "shadow_run": bool(config.get("execution_policy", {}).get("shadow_run", False)),
    }
    payload = json.dumps(config, ensure_ascii=False, indent=2)
    alias_paths = runtime_config_alias_paths(PACKAGE_ROOT / "configs", resolved_profile)
    for alias_path in alias_paths:
        _atomic_write_text(alias_path, payload, encoding="utf-8")
    return alias_paths[0]


def _effective_config_path(
    explicit_path: str,
    profile: str,
    execution_mode: str = "",
    precision_trade: str = "default",
    execution_namespace: str = "",
    ignore_market_panic_reduce_only: str = "default",
    allow_unfinished_orders_reconcile: str = "default",
    shadow_run: bool = False,
) -> Path:
    if str(explicit_path).strip():
        return Path(explicit_path).resolve()
    return _write_runtime_config(
        profile,
        execution_mode=execution_mode,
        precision_trade=precision_trade,
        execution_namespace=execution_namespace,
        ignore_market_panic_reduce_only=ignore_market_panic_reduce_only,
        allow_unfinished_orders_reconcile=allow_unfinished_orders_reconcile,
        shadow_run=shadow_run,
    )


def _print_stage_preview(mode: str, config: Dict[str, Any]) -> None:
    print("Stage Preview:")
    for idx, stage in enumerate(runtime_stage_preview(mode=mode, config=config), start=1):
        print(f"  {idx}. {stage}")


def _emit_result_json(payload: Dict[str, Any]) -> None:
    print("===== ASHARE RESULT JSON START =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("===== ASHARE RESULT JSON END =====")


def _result_exit_code(mode: str, payload: Dict[str, Any]) -> int:
    if str(mode or "") != "execution_only":
        return 0
    status = str(payload.get("status", "") or "").strip().lower()
    ok = bool(payload.get("ok", False))
    scheduler_verdict = str(dict(payload.get("scheduler_verdict", {}) or {}).get("final_verdict", "") or "").strip().lower()
    if status in {"gate_only", "skipped", "blocked"}:
        return 0
    if scheduler_verdict in {"defer", "block"} and status != "execution_error":
        return 0
    if status == "execution_error" or not ok:
        return 2
    return 0


def main() -> None:
    args = parse_args()
    config_path = _effective_config_path(
        args.config,
        args.profile,
        execution_mode=str(args.execution_mode).strip(),
        precision_trade=str(args.precision_trade).strip(),
        execution_namespace=str(args.execution_namespace).strip(),
        ignore_market_panic_reduce_only=str(args.ignore_market_panic_reduce_only).strip(),
        allow_unfinished_orders_reconcile=str(args.allow_unfinished_orders_reconcile).strip(),
        shadow_run=bool(args.shadow_run),
    )
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    snapshot_path = write_control_plane_snapshot(Path(__file__).resolve().parent)

    print("===== ASHARE START =====")
    print("Config:", config_path)
    print("Mode:", args.mode)
    print("Profile:", args.profile)
    print("Research cycles:", config.get("supervisor", {}).get("gpu_research_max_cycles_per_tick"))
    print("Plan reuse hours:", config.get("supervisor", {}).get("token_plan_min_interval_hours"))
    print("Execution mode:", config.get("execution_policy", {}).get("account_mode"))
    print("Precision trade:", config.get("execution_policy", {}).get("precision_trade_enabled"))
    print("Execution namespace:", config.get("execution_policy", {}).get("namespace"))
    print("Ignore PANIC reduce-only:", config.get("execution_policy", {}).get("ignore_market_panic_reduce_only"))
    print("Allow unfinished reconcile:", config.get("execution_policy", {}).get("allow_unfinished_orders_reconcile"))
    print("Shadow run:", config.get("execution_policy", {}).get("shadow_run"))
    print("Log root:", config.get("paths", {}).get("log_root"))
    print("Control plane snapshot:", snapshot_path)
    print("Supervisor state:", Path(str(config.get("paths", {}).get("research_root", ""))) / "supervisor" / "supervisor_state.json")
    _print_stage_preview(mode=args.mode, config=config)

    if args.mode == "integrated_supervisor":
        from engine.supervisor import run_integrated_supervisor

        run_integrated_supervisor(config_path)
    elif args.mode == "research_only":
        from engine.supervisor import run_research_only

        run_research_only(config_path)
    elif args.mode == "release_only":
        from engine.supervisor import run_release_only

        release = run_release_only(
            config_path,
            source_mode=str(args.release_source_mode).strip() or "release_only",
            summary_path=str(args.source_summary_path).strip(),
            target_positions_path=str(args.source_target_positions_path).strip(),
            note=str(args.release_note).strip(),
            forced_trade_date=str(args.release_trade_date).strip(),
        )
        print("Latest release:", release.get("release_id"))
        print("Trade date:", release.get("trade_date"))
        print("Manifest:", release.get("artifacts", {}).get("manifest_path"))
        _emit_result_json(release)
    elif args.mode == "execution_only":
        from engine.execution_manager import run_execution_only

        result = run_execution_only(
            config_path=config_path,
            release_id=str(args.release_id).strip(),
            ignore_window=bool(args.ignore_window),
            gate_only=bool(args.gate_only),
            trigger_label="manual",
            trigger_source="main_research_runner",
        )
        _emit_result_json(result)
        raise SystemExit(_result_exit_code(args.mode, result))
    elif args.mode == "midday_review_only":
        from engine.midday_review import run_midday_review

        result = run_midday_review(
            config_path=config_path,
            release_id=str(args.release_id).strip(),
        )
        _emit_result_json(result)
    elif args.mode == "intraday_tactics_only":
        from engine.intraday_tactics.runtime import run_intraday_tactics_pipeline

        result = run_intraday_tactics_pipeline(
            config_path,
            tactical_phase=str(args.tactical_phase or "manual").strip() or "manual",
            execute=not bool(args.tactical_no_execute),
            gate_only=False,
            trade_date=str(args.release_trade_date or "").strip(),
        )
        _emit_result_json(result)
    elif args.mode == "evidence_audit_only":
        from engine.evidence_audit import run_evidence_audit

        result = run_evidence_audit(
            config,
            candidate_pool_path=Path(str(args.candidate_pool_path)).resolve() if str(args.candidate_pool_path).strip() else None,
            limit=int(args.evidence_limit or 0) or None,
        )
        _emit_result_json(result)
    elif args.mode == "oms_validate":
        from engine.oms.validation import run_oms_validation_suite

        result = run_oms_validation_suite(config=config)
        _emit_result_json(result)
    elif args.mode == "resume_downstream":
        from engine.supervisor import run_resume_downstream

        run_resume_downstream(config_path, include_execution=bool(args.resume_execution))
    else:
        from engine.orchestrator import run_cycle as run_legacy_cycle

        run_legacy_cycle(config_path=config_path, mode=args.mode)
    print("===== ASHARE DONE =====")


if __name__ == "__main__":
    main()
