from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

VALID_ACCOUNT_MODES = {"simulation", "precision"}


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
    return path


def _parse_bridge_stdout(stdout: str, default_ok: bool = False) -> Dict[str, Any]:
    text = str(stdout or "").strip()
    if not text:
        return {"ok": bool(default_ok), "stdout": ""}
    try:
        payload = json.loads(text)
    except Exception:
        return {
            "ok": False,
            "stdout": text,
            "parse_error": "bridge_stdout_not_json",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "stdout": text,
            "parse_error": "bridge_stdout_not_object",
        }
    if "ok" not in payload:
        inferred_ok = bool(
            payload.get("execution_report_path")
            or payload.get("timestamp")
            or payload.get("oms")
            or payload.get("portfolio_control")
        )
        payload["ok"] = inferred_ok if inferred_ok else bool(default_ok)
    if "status" not in payload and bool(payload.get("ok", False)):
        payload["status"] = "shadow_executed" if bool(payload.get("shadow_run", False)) else "executed"
    return payload


def execution_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(config.get("execution_policy", {}) or {})
    mode = str(raw.get("account_mode", "simulation") or "simulation").strip().lower()
    if mode not in VALID_ACCOUNT_MODES:
        mode = "simulation"
    namespace = str(raw.get("namespace", "main") or "main").strip() or "main"
    return {
        "account_mode": mode,
        "precision_trade_enabled": bool(raw.get("precision_trade_enabled", False)),
        "allow_integrated_precision_execution": bool(raw.get("allow_integrated_precision_execution", False)),
        "ignore_market_panic_reduce_only": bool(raw.get("ignore_market_panic_reduce_only", False)),
        "allow_unfinished_orders_reconcile": bool(raw.get("allow_unfinished_orders_reconcile", False)),
        "namespace": namespace,
        "shadow_run": bool(raw.get("shadow_run", False)),
    }


def _apply_account_profile(payload: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    broker_cfg = dict(payload.get("broker", {}) or {})
    account_profiles = dict(broker_cfg.get("account_profiles", {}) or {})
    selected = dict(account_profiles.get(str(policy.get("account_mode", "simulation")), {}) or {})
    if selected:
        broker_cfg["account_id"] = str(selected.get("account_id", broker_cfg.get("account_id", "")) or broker_cfg.get("account_id", ""))
        broker_cfg["account_alias"] = str(selected.get("account_alias", broker_cfg.get("account_alias", "")) or broker_cfg.get("account_alias", ""))
    broker_cfg["selected_account_mode"] = str(policy.get("account_mode", "simulation"))
    payload["broker"] = broker_cfg
    payload["execution_policy"] = policy
    return payload


def build_execution_runtime_config(
    config: Dict[str, Any],
    explicit_portfolio_path: str = "",
    release_context: Dict[str, Any] | None = None,
    intraday_tactical_orders_path: str = "",
) -> Path:
    exec_cfg = dict(config.get("execution_bridge", {}) or {})
    template_path = Path(str(exec_cfg["config_template_path"]))
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    policy = execution_policy(config)
    portfolio_root = str(config["paths"].get("portfolio_output_root", payload.get("portfolio_root", "")))
    payload["portfolio_root"] = portfolio_root
    payload["explicit_portfolio_path"] = str(Path(explicit_portfolio_path).resolve()) if str(explicit_portfolio_path).strip() else str(Path(portfolio_root) / "target_positions.csv")
    payload["price_snapshot_path"] = str(config.get("market_pipeline", {}).get("price_snapshot_path", payload.get("price_snapshot_path", "")))
    namespace = str(policy.get("namespace", "main") or "main").strip() or "main"
    live_execution_root = Path(str(config["paths"].get("live_execution_root", payload.get("output_dir", "")))).resolve()
    payload["output_dir"] = str(live_execution_root if namespace == "main" else live_execution_root / namespace)
    control_cfg = dict(config.get("portfolio_control", {}) or {})
    if namespace != "main":
        control_cfg["enable_dev_log_snapshot"] = False
    control_cfg.setdefault("codex_dev_log_path", str(Path(__file__).resolve().parents[3] / "CODEX_DEV_LOG.md"))
    payload["portfolio_control"] = control_cfg
    oms_cfg = dict(config.get("oms", {}) or {})
    if namespace != "main":
        base_oms_root = Path(str(oms_cfg.get("output_root", config.get("paths", {}).get("oms_output_root", live_execution_root / "oms_v1")))).resolve()
        oms_cfg["output_root"] = str(base_oms_root / namespace)
    payload["oms"] = oms_cfg
    if release_context:
        payload["release"] = release_context
    tac_path = str(intraday_tactical_orders_path or config.get("intraday_tactical_orders_path", "") or "").strip()
    if tac_path:
        payload["intraday_tactical_orders_path"] = str(Path(tac_path).resolve())
    payload = _apply_account_profile(payload=payload, policy=policy)
    out_path = Path(str(exec_cfg["autogen_config_path"]))
    runtime_root = out_path.parent / "generated_runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_path = runtime_root / f"{out_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(out_path, text, encoding="utf-8")
    _atomic_write_text(runtime_path, text, encoding="utf-8")
    return runtime_path


def run_execution_bridge(
    config: Dict[str, Any],
    project_root: Path,
    explicit_portfolio_path: str = "",
    release_context: Dict[str, Any] | None = None,
    intraday_tactical_orders_path: str = "",
) -> Dict[str, Any]:
    exec_cfg = dict(config.get("execution_bridge", {}) or {})
    tac = str(intraday_tactical_orders_path or config.get("intraday_tactical_orders_path", "") or "").strip()
    runtime_config_path = build_execution_runtime_config(
        config=config,
        explicit_portfolio_path=explicit_portfolio_path,
        release_context=release_context,
        intraday_tactical_orders_path=tac,
    )
    pyexe = str(exec_cfg["python_executable"])
    script = Path(str(exec_cfg["script_path"]))
    env = os.environ.copy()
    proc = subprocess.run(
        [pyexe, str(script), "--config", str(runtime_config_path)],
        cwd=str(project_root),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = (proc.stdout or "").strip()
    report = _parse_bridge_stdout(stdout, default_ok=False)
    report.setdefault("runtime_config_path", str(runtime_config_path))
    if release_context:
        report.setdefault("release", release_context)
    return report


def run_execution_health_probe(
    config: Dict[str, Any],
    project_root: Path,
) -> Dict[str, Any]:
    exec_cfg = dict(config.get("execution_bridge", {}) or {})
    runtime_config_path = build_execution_runtime_config(config=config)
    pyexe = str(exec_cfg["python_executable"])
    script = Path(str(exec_cfg["health_probe_script_path"]))
    env = os.environ.copy()
    proc = subprocess.run(
        [pyexe, str(script), "--config", str(runtime_config_path)],
        cwd=str(project_root),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = (proc.stdout or "").strip()
    report = _parse_bridge_stdout(stdout, default_ok=False)
    report.setdefault("runtime_config_path", str(runtime_config_path))
    return report
