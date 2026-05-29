from __future__ import annotations

import argparse
import importlib
import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


def _load_json_yaml(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _append_check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def _compile_file(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def _subprocess_import_check(python_executable: Path, runtime_root: Path, module_name: str) -> subprocess.CompletedProcess[str]:
    command = [
        str(python_executable),
        "-c",
        (
            "import importlib, sys; "
            f"sys.path.insert(0, r'{runtime_root}'); "
            f"importlib.import_module('{module_name}')"
        ),
    ]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(exc))


def _command_exists(command_name: str) -> bool:
    text = str(command_name or "").strip()
    if not text:
        return False
    candidate = Path(text)
    if candidate.exists():
        return True
    return shutil.which(text) is not None


def run_preflight(repo_root: Path, profile: str, mode: str, explicit_config: str = "") -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    manifest_path = repo_root / "SYSTEM_MANIFEST.yaml"
    profiles_path = repo_root / "RUN_PROFILES.yaml"
    manifest = _load_json_yaml(manifest_path)
    profiles_doc = _load_json_yaml(profiles_path)

    main_path = Path(str(manifest["canonical"]["wrapped_business_root_entry"]))
    canonical = dict(manifest.get("canonical", {}) or {})
    runtime_root = Path(str(canonical.get("workspace_runtime_root") or canonical.get("live_runtime_root") or ""))
    formal_output_root = Path(str(manifest["canonical"]["formal_output_root"]))

    _append_check(checks, "manifest_exists", manifest_path.exists(), str(manifest_path))
    _append_check(checks, "profiles_exists", profiles_path.exists(), str(profiles_path))
    _append_check(checks, "main_exists", main_path.exists(), str(main_path))
    _append_check(checks, "runtime_root_exists", runtime_root.exists(), str(runtime_root))
    _append_check(checks, "formal_output_parent_exists", formal_output_root.parent.exists(), str(formal_output_root.parent))

    allowed_profiles = dict(profiles_doc.get("allowed_profiles", {}) or {})
    allowed_modes = list(profiles_doc.get("allowed_modes", []) or [])
    _append_check(checks, "profile_allowed", profile in allowed_profiles, profile)
    _append_check(checks, "mode_allowed", mode in allowed_modes, mode)

    if explicit_config:
        config_path = Path(explicit_config).resolve()
        _append_check(checks, "explicit_config_exists", config_path.exists(), str(config_path))
    else:
        config_path = None

    compile_targets = [
        repo_root / "launch_canonical.py",
        repo_root / "main_research_runner.py",
        repo_root / "trade_clock_service.py",
        repo_root / "tools" / "preflight_check.py",
        repo_root / "scripts" / "update_affordable_data_bundle.py",
        repo_root / "scripts" / "build_event_fact_layer.py",
        repo_root / "scripts" / "build_industry_hard_factor_layer.py",
        repo_root / "scripts" / "update_external_research_feeds.py",
        repo_root / "scripts" / "build_audit_site_index.py",
        runtime_root / "engine" / "local_settings.py",
        runtime_root / "engine" / "config_builder.py",
        runtime_root / "engine" / "supervisor.py",
        runtime_root / "engine" / "portfolio_release.py",
        runtime_root / "engine" / "trading_clock.py",
        runtime_root / "engine" / "execution_manager.py",
        runtime_root / "engine" / "clock_supervisor.py",
        runtime_root / "engine" / "intraday_proxy_store.py",
    ]
    for target in compile_targets:
        try:
            _compile_file(target)
            _append_check(checks, f"py_compile:{target.name}", True, str(target))
        except Exception as exc:
            _append_check(checks, f"py_compile:{target.name}", False, str(exc))

    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    import_targets = [
        "engine.local_settings",
        "engine.config_builder",
        "engine.portfolio_release",
        "engine.execution_manager",
    ]
    for target in import_targets:
        try:
            importlib.import_module(target)
            _append_check(checks, f"import:{target}", True, "ok")
        except Exception as exc:
            _append_check(checks, f"import:{target}", False, str(exc))

    try:
        local_settings = importlib.import_module("engine.local_settings")
        research_python = Path(str(getattr(local_settings, "PYTHON_EXECUTABLE", "") or "").strip())
    except Exception as exc:
        research_python = Path("__missing_python__")
        _append_check(checks, "canonical_research_python", False, str(exc))
    else:
        _append_check(checks, "canonical_research_python", research_python.exists(), str(research_python))

    if research_python.exists():
        proc = _subprocess_import_check(
            python_executable=research_python,
            runtime_root=runtime_root,
            module_name="engine.supervisor",
        )
        if proc.returncode == 0:
            _append_check(checks, "import:engine.supervisor@canonical_python", True, "ok")
        else:
            detail = (proc.stderr or proc.stdout or "import failed").strip()
            _append_check(checks, "import:engine.supervisor@canonical_python", False, detail)

    if config_path and config_path.exists():
        try:
            runtime_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _append_check(checks, "runtime_config_loadable", False, str(exc))
        else:
            _append_check(checks, "runtime_config_loadable", True, str(config_path))

            affordable_cfg = dict(runtime_config.get("affordable_data_bundle", {}) or {})
            if bool(affordable_cfg.get("enabled", True)) and bool(affordable_cfg.get("run_before_research", True)):
                affordable_script = Path(str(affordable_cfg.get("script_path", "") or "")).resolve()
                _append_check(checks, "affordable_script_exists", affordable_script.exists(), str(affordable_script))

            research_fact_cfg = dict(runtime_config.get("research_fact_refresh", {}) or {})
            if bool(research_fact_cfg.get("enabled", True)) and bool(research_fact_cfg.get("run_before_research", True)):
                event_script = Path(str(research_fact_cfg.get("event_script_path", "") or "")).resolve()
                hard_factor_script = Path(str(research_fact_cfg.get("hard_factor_script_path", "") or "")).resolve()
                _append_check(checks, "research_fact_event_script_exists", event_script.exists(), str(event_script))
                _append_check(checks, "research_fact_hard_factor_script_exists", hard_factor_script.exists(), str(hard_factor_script))

            external_cfg = dict(runtime_config.get("external_research_refresh", {}) or {})
            if bool(external_cfg.get("enabled", True)) and bool(external_cfg.get("run_before_research", True)):
                external_script = Path(str(external_cfg.get("script_path", "") or "")).resolve()
                external_seed = Path(str(external_cfg.get("seed_path", "") or "")).resolve()
                _append_check(checks, "external_research_script_exists", external_script.exists(), str(external_script))
                _append_check(checks, "external_research_seed_exists", external_seed.exists(), str(external_seed))

            publish_cfg = dict(runtime_config.get("audit_site_publish", {}) or {})
            if bool(publish_cfg.get("enabled", True)) and bool(publish_cfg.get("run_after_summary", True)):
                publish_script = Path(str(publish_cfg.get("script_path", "") or "")).resolve()
                build_index_script = publish_script.parent / "build_audit_site_index.py"
                powershell_exe = str(publish_cfg.get("powershell_executable", "powershell.exe") or "powershell.exe")
                publish_python = str(publish_cfg.get("python_executable", "") or "")
                _append_check(checks, "audit_publish_script_exists", publish_script.exists(), str(publish_script))
                _append_check(checks, "audit_publish_index_builder_exists", build_index_script.exists(), str(build_index_script))
                _append_check(checks, "audit_publish_powershell_exists", _command_exists(powershell_exe), powershell_exe)
                _append_check(checks, "audit_publish_python_exists", _command_exists(publish_python), publish_python)
                _append_check(checks, "audit_publish_ssh_exists", _command_exists("ssh"), "ssh")
                _append_check(checks, "audit_publish_scp_exists", _command_exists("scp"), "scp")

    ok = all(bool(item["ok"]) for item in checks)
    return {
        "ok": ok,
        "repo_root": str(repo_root),
        "mode": mode,
        "profile": profile,
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lightweight governance preflight checks")
    parser.add_argument("--repo-root", default="", help="Repository root; defaults to the parent of this tools directory")
    parser.add_argument("--profile", required=True, help="Profile to validate")
    parser.add_argument("--mode", required=True, help="Mode to validate")
    parser.add_argument("--config", default="", help="Optional explicit runtime config path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if str(args.repo_root).strip() else Path(__file__).resolve().parents[1]
    report = run_preflight(
        repo_root=repo_root,
        profile=str(args.profile).strip(),
        mode=str(args.mode).strip(),
        explicit_config=str(args.config).strip(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if bool(report.get("ok", False)) else 1)


if __name__ == "__main__":
    main()
