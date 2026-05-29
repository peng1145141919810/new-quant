from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


SAFE_REMOTE_PHASES = {"research", "release", "research_refresh", "release_refresh", "midday_review", "summary"}


def remote_delegate_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(dict(config.get("trade_clock", {}) or {}).get("remote_delegate", {}) or {})


def should_delegate_phase(config: Dict[str, Any], phase_name: str) -> bool:
    cfg = remote_delegate_cfg(config)
    if not bool(cfg.get("enabled", False)):
        return False
    allowed = {str(item).strip() for item in list(cfg.get("phases", []) or []) if str(item).strip()}
    return phase_name in SAFE_REMOTE_PHASES and phase_name in allowed


def _rewrite_command_for_remote(command: List[str], local_repo_root: Path, remote_repo_root: str, remote_python: str) -> List[str]:
    local_root = str(local_repo_root.resolve())
    rewritten: List[str] = []
    for idx, item in enumerate(command):
        text = str(item)
        if idx == 0:
            rewritten.append(str(remote_python or text))
            continue
        if text.startswith(local_root):
            suffix = text[len(local_root):].lstrip("\\/")
            rewritten.append(str(Path(remote_repo_root) / Path(suffix)))
        else:
            rewritten.append(text)
    return rewritten


def run_remote_phase(
    *,
    config: Dict[str, Any],
    phase_name: str,
    command: List[str],
    local_repo_root: Path,
    stdout_log: Path,
    stderr_log: Path,
    timeout_minutes: int,
) -> Dict[str, Any]:
    cfg = remote_delegate_cfg(config)
    remote_user = str(cfg.get("remote_user", "ubuntu") or "ubuntu").strip()
    remote_host = str(cfg.get("remote_host", "") or "").strip()
    remote_repo_root = str(cfg.get("remote_repo_root", "/opt/ashare_runtime") or "/opt/ashare_runtime").strip()
    remote_python = str(cfg.get("python_executable", "/usr/bin/python3") or "/usr/bin/python3").strip()
    ssh_options = [str(item) for item in list(cfg.get("ssh_options", []) or []) if str(item).strip()]
    fallback_to_local = bool(cfg.get("fallback_to_local", True))
    if not remote_host:
        return {"delegated": False, "fallback_to_local": fallback_to_local, "error": "remote_host_missing"}
    remote_command = _rewrite_command_for_remote(command, local_repo_root=local_repo_root, remote_repo_root=remote_repo_root, remote_python=remote_python)
    quoted = " ".join(shlex.quote(item) for item in remote_command)
    ssh_target = f"{remote_user}@{remote_host}"
    wrapped = f"cd {shlex.quote(remote_repo_root)} && {quoted}"
    ssh_command = ["ssh", *ssh_options, ssh_target, wrapped]
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open("w", encoding="utf-8") as stderr_handle:
        try:
            completed = subprocess.run(
                ssh_command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                timeout=max(int(timeout_minutes or 0), 1) * 60,
                check=False,
            )
            return {
                "delegated": True,
                "ok": completed.returncode == 0,
                "return_code": completed.returncode,
                "elapsed_seconds": round(time.time() - started, 3),
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
                "fallback_to_local": fallback_to_local,
                "remote_target": ssh_target,
                "phase_name": phase_name,
            }
        except subprocess.TimeoutExpired:
            stderr_handle.write(f"remote_phase_timeout:{phase_name}\n")
            return {
                "delegated": True,
                "ok": False,
                "return_code": None,
                "elapsed_seconds": round(time.time() - started, 3),
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
                "fallback_to_local": fallback_to_local,
                "remote_target": ssh_target,
                "phase_name": phase_name,
                "error": "remote_phase_timeout",
            }
        except Exception as exc:
            stderr_handle.write(f"remote_phase_error:{exc}\n")
            return {
                "delegated": True,
                "ok": False,
                "return_code": None,
                "elapsed_seconds": round(time.time() - started, 3),
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
                "fallback_to_local": fallback_to_local,
                "remote_target": ssh_target,
                "phase_name": phase_name,
                "error": str(exc),
            }
