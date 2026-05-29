from __future__ import annotations

import copy
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable

from .clock_phase_registry import phase_sequence, tactical_phase_map, tactical_phase_names
from .clock_account_snapshot import build_clock_account_snapshot
from .config_utils import ensure_dir, load_config
from .data_consistency_guard import assess_automation_data_readiness
from .derived_alpha_refresh import run_derived_alpha_refresh
from .execution_manager import assess_execution_gate
from .intraday_proxy_store import build_intraday_proxy_snapshot
from .intraday_state_machine import refresh_intraday_state_machine
from .market_pipeline import build_daily_price_snapshot, run_market_pipeline
from .portfolio_release import load_latest_release, load_release_by_id
from .remote_clock_delegate import run_remote_phase, should_delegate_phase
from .safety_guard import assess_system_safety
from .sql_store import ensure_schema, load_runtime_json_artifact, resolve_sqlite_path, sql_store_enabled, sqlite_connection, upsert_runtime_json_artifact
from .strategy_audit import build_strategy_audit_pack
from .tushare_client import TushareClient
from .trading_clock import clock_now, is_trading_day, next_trading_day, trading_clock_snapshot
FINAL_PHASE_STATUSES = {"success", "failed", "skipped", "timeout"}
_PHASES_REQUIRING_PRE_INTRADAY_REFRESH = frozenset(
    {"preopen_gate", "simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow"}
)
RESULT_START = "===== ASHARE RESULT JSON START ====="
RESULT_END = "===== ASHARE RESULT JSON END ====="


class RuntimeReloadRequested(RuntimeError):
    pass


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    lock_name: str
    scheduled_time: str
    timeout_minutes: int


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve())


def _automation_runs_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("automation_runs_root", "") or "")).resolve())


def _phase_state_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(_trade_clock_root(config) / "phase_state")


def _locks_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(_trade_clock_root(config) / "locks")


def _runtime_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(_trade_clock_root(config) / "runtime")


def _clock_state_path(config: Dict[str, Any]) -> Path:
    return _trade_clock_root(config) / "clock_state.json"


def _scheduler_runtime_state_path(config: Dict[str, Any]) -> Path:
    return _runtime_root(config) / "scheduler_runtime.json"


def _stop_request_path(config: Dict[str, Any]) -> Path:
    return _runtime_root(config) / "stop_request.json"


def _phase_runtime_dir(config: Dict[str, Any], trade_date: str) -> Path:
    return ensure_dir(_runtime_root(config) / trade_date.replace("-", ""))


def _runtime_log_paths(config: Dict[str, Any], trade_date: str, phase_name: str) -> Dict[str, Path]:
    root = _phase_runtime_dir(config, trade_date)
    return {
        "root": root,
        "stdout": root / f"{phase_name}.stdout.log",
        "stderr": root / f"{phase_name}.stderr.log",
    }


def _research_python_from_config(config: Dict[str, Any]) -> str:
    configured = str(dict(config.get("execution", {}) or {}).get("python_executable", "") or "").strip()
    return configured or sys.executable


def _phase_state_path(config: Dict[str, Any], trade_date: str) -> Path:
    return _phase_state_root(config) / f"{trade_date.replace('-', '')}.json"


def _lock_path(config: Dict[str, Any], lock_name: str) -> Path:
    return _locks_root(config) / f"{lock_name}.lock.json"


def _load_json(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    fallback = dict(default or {})
    config = getattr(_load_json, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if isinstance(payload, dict) and payload:
                    return payload
            except Exception:
                pass
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    config = getattr(_write_json, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        with sqlite_connection(resolve_sqlite_path(config)) as conn:
            ensure_schema(conn)
            upsert_runtime_json_artifact(conn, path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    last_err: OSError | PermissionError | None = None
    for attempt in range(5):
        try:
            tmp_path.write_text(text, encoding="utf-8")
            os.replace(tmp_path, path)
            return path
        except (PermissionError, OSError) as exc:
            last_err = exc
            time.sleep(0.05 * (attempt + 1))
    if last_err:
        raise last_err
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)
    return path


def _scheduler_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(dict(config.get("trade_clock", {}) or {}).get("scheduler", {}) or {})


def _hot_reload_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(dict(config.get("trade_clock", {}) or {}).get("runtime_hot_reload", {}) or {})


def _iter_hot_reload_candidate_paths(config: Dict[str, Any], config_path: Path) -> Iterable[Path]:
    repo_root = _repo_root()
    project_root = repo_root / "src" / "ashare"
    for path in [
        Path(config_path).resolve(),
        repo_root / "trade_clock_service.py",
        repo_root / "launch_canonical.py",
        repo_root / "main_research_runner.py",
        project_root / "engine" / "local_settings.py",
        project_root / "engine" / "local_settings.example.py",
    ]:
        yield path
    hot_cfg = _hot_reload_cfg(config)
    for root in [
        Path(str(hot_cfg.get("watch_scripts_root", repo_root / "scripts"))),
        Path(str(hot_cfg.get("watch_hub_root", project_root / "engine"))),
        Path(str(hot_cfg.get("watch_bridge_root", project_root / "live_execution_bridge"))),
        Path(str(hot_cfg.get("watch_csharp_root", repo_root / "csharp_runtime_skeleton"))),
    ]:
        root = root.resolve()
        if not root.exists():
            continue
        yield root


def _runtime_watch_fingerprint(config: Dict[str, Any], config_path: Path) -> Dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    latest_mtime_ns = 0
    suffixes = {".py", ".json", ".yaml", ".yml", ".cs", ".ps1", ".md"}
    for candidate in _iter_hot_reload_candidate_paths(config, config_path):
        if candidate.is_file():
            paths = [candidate]
        elif candidate.is_dir():
            paths = [
                path
                for path in candidate.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.lower() in suffixes
            ]
        else:
            continue
        for path in sorted(paths):
            try:
                stat = path.stat()
            except Exception:
                continue
            file_count += 1
            latest_mtime_ns = max(latest_mtime_ns, int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))))
            digest.update(str(path).encode("utf-8", errors="ignore"))
            digest.update(str(latest_mtime_ns).encode("utf-8"))
            digest.update(str(stat.st_size).encode("utf-8"))
    return {
        "file_count": file_count,
        "latest_mtime_ns": latest_mtime_ns,
        "digest": digest.hexdigest(),
    }


def _scheduler_phase_cfg(config: Dict[str, Any], phase_name: str) -> Dict[str, Any]:
    scheduler = _scheduler_cfg(config)
    return dict(dict(scheduler.get("phases", {}) or {}).get(phase_name, {}) or {})


def _scheduler_bool(scheduler: Dict[str, Any], primary_key: str, legacy_key: str = "", default: bool = False) -> bool:
    if primary_key in scheduler:
        return bool(scheduler.get(primary_key, default))
    if legacy_key and legacy_key in scheduler:
        return bool(scheduler.get(legacy_key, default))
    return bool(default)


def _phase_specs(config: Dict[str, Any]) -> Dict[str, PhaseSpec]:
    research_cfg = _scheduler_phase_cfg(config, "research")
    release_cfg = _scheduler_phase_cfg(config, "release")
    research_refresh_cfg = _scheduler_phase_cfg(config, "research_refresh")
    release_refresh_cfg = _scheduler_phase_cfg(config, "release_refresh")
    preopen_cfg = _scheduler_phase_cfg(config, "preopen_gate")
    simulation_cfg = _scheduler_phase_cfg(config, "simulation")
    shadow_cfg = _scheduler_phase_cfg(config, "shadow")
    midday_cfg = _scheduler_phase_cfg(config, "midday_review")
    afternoon_exec_cfg = _scheduler_phase_cfg(config, "afternoon_execution")
    afternoon_shadow_cfg = _scheduler_phase_cfg(config, "afternoon_shadow")
    summary_cfg = _scheduler_phase_cfg(config, "summary")
    tac_sched = tactical_phase_map(config)
    base = {
        "research": PhaseSpec("research", "research", str(research_cfg.get("time", "15:05:00") or "15:05:00"), int(research_cfg.get("timeout_minutes", 420) or 420)),
        "release": PhaseSpec("release", "release", str(release_cfg.get("time", "15:10:00") or "15:10:00"), int(release_cfg.get("timeout_minutes", 30) or 30)),
        "research_refresh": PhaseSpec("research_refresh", "research", str(research_refresh_cfg.get("time", "08:35:00") or "08:35:00"), int(research_refresh_cfg.get("timeout_minutes", 120) or 120)),
        "release_refresh": PhaseSpec("release_refresh", "release", str(release_refresh_cfg.get("time", "08:55:00") or "08:55:00"), int(release_refresh_cfg.get("timeout_minutes", 20) or 20)),
        "preopen_gate": PhaseSpec("preopen_gate", "execution", str(preopen_cfg.get("time", "09:20:00") or "09:20:00"), int(preopen_cfg.get("timeout_minutes", 15) or 15)),
        "simulation": PhaseSpec("simulation", "simulation", str(simulation_cfg.get("time", "09:30:35") or "09:30:35"), int(simulation_cfg.get("timeout_minutes", 45) or 45)),
        "shadow": PhaseSpec("shadow", "shadow", str(shadow_cfg.get("time", "09:35:00") or "09:35:00"), int(shadow_cfg.get("timeout_minutes", 30) or 30)),
        "midday_review": PhaseSpec("midday_review", "midday_review", str(midday_cfg.get("time", "11:35:00") or "11:35:00"), int(midday_cfg.get("timeout_minutes", 10) or 10)),
        "afternoon_execution": PhaseSpec("afternoon_execution", "afternoon_execution", str(afternoon_exec_cfg.get("time", "13:05:00") or "13:05:00"), int(afternoon_exec_cfg.get("timeout_minutes", 30) or 30)),
        "afternoon_shadow": PhaseSpec("afternoon_shadow", "afternoon_shadow", str(afternoon_shadow_cfg.get("time", "13:15:00") or "13:15:00"), int(afternoon_shadow_cfg.get("timeout_minutes", 20) or 20)),
        "summary": PhaseSpec("summary", "summary", str(summary_cfg.get("time", "15:20:00") or "15:20:00"), int(summary_cfg.get("timeout_minutes", 20) or 20)),
    }
    for name, row in tac_sched.items():
        item = dict(row or {})
        base[str(name)] = PhaseSpec(
            str(name),
            str(name),
            str(item.get("time", "10:00:00") or "10:00:00"),
            int(item.get("timeout_minutes", 8) or 8),
        )
    return base


def _empty_phase_state() -> Dict[str, Any]:
    return {
        "status": "queued",
        "scheduled_for": "",
        "started_at": "",
        "finished_at": "",
        "return_code": None,
        "release_id": "",
        "warning_count": 0,
        "error_message": "",
        "stdout_log": "",
        "stderr_log": "",
        "stdout_tail": [],
        "stderr_tail": [],
        "result_status": "",
        "result_payload": {},
    }


def _phase_sequence(config: Dict[str, Any]) -> list[str]:
    return phase_sequence(config)


def _ensure_cycle_state(config: Dict[str, Any], trade_date: str, profile: str) -> Dict[str, Any]:
    _load_json._active_config = config
    _write_json._active_config = config
    path = _phase_state_path(config, trade_date)
    state = _load_json(path, default={})
    phases = dict(state.get("phases", {}) or {})
    for phase_name in _phase_sequence(config):
        bucket = dict(phases.get(phase_name, {}) or {})
        default_bucket = _empty_phase_state()
        default_bucket.update(bucket)
        phases[phase_name] = default_bucket
    state.update(
        {
            "date": str(trade_date),
            "scheduler_profile": str(profile or ""),
            "updated_at": clock_now().isoformat(timespec="seconds"),
            "release_id": str(state.get("release_id", "") or ""),
            "fallback": dict(state.get("fallback", {}) or {}),
            "phases": phases,
        }
    )
    if "created_at" not in state:
        state["created_at"] = state["updated_at"]
    _write_json(path, state)
    return state


def _save_cycle_state(config: Dict[str, Any], state: Dict[str, Any]) -> Path:
    _write_json._active_config = config
    state["updated_at"] = clock_now().isoformat(timespec="seconds")
    return _write_json(_phase_state_path(config, str(state.get("date", "") or "")), state)


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                ctypes.windll.kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(config: Dict[str, Any], lock_name: str, trade_date: str, phase_name: str) -> Dict[str, Any] | None:
    path = _lock_path(config, lock_name)
    current = _load_json(path, default={})
    owner_pid = int(current.get("owner_pid", 0) or 0)
    child_pid = int(current.get("child_pid", 0) or 0)
    if current and (_process_alive(child_pid) or _process_alive(owner_pid)):
        return None
    if path.exists():
        path.unlink(missing_ok=True)
    payload = {
        "phase_name": str(phase_name),
        "trade_date": str(trade_date),
        "owner_pid": os.getpid(),
        "child_pid": 0,
        "acquired_at": clock_now().isoformat(timespec="seconds"),
    }
    _write_json(path, payload)
    return payload


def _update_lock_child_pid(config: Dict[str, Any], lock_name: str, child_pid: int) -> None:
    path = _lock_path(config, lock_name)
    current = _load_json(path, default={})
    if not current:
        return
    current["child_pid"] = int(child_pid or 0)
    current["updated_at"] = clock_now().isoformat(timespec="seconds")
    _write_json(path, current)


def _release_lock(config: Dict[str, Any], lock_name: str) -> None:
    path = _lock_path(config, lock_name)
    if not path.exists():
        return
    current = _load_json(path, default={})
    owner_pid = int(current.get("owner_pid", 0) or 0)
    if owner_pid and owner_pid != os.getpid():
        return
    path.unlink(missing_ok=True)


def _clear_owned_locks(config: Dict[str, Any]) -> None:
    for lock_file in _locks_root(config).glob("*.lock.json"):
        current = _load_json(lock_file, default={})
        if int(current.get("owner_pid", 0) or 0) != os.getpid():
            continue
        child_pid = int(current.get("child_pid", 0) or 0)
        if _process_alive(child_pid):
            continue
        lock_file.unlink(missing_ok=True)


def _parse_time(value: str) -> datetime.time:
    return datetime.strptime(str(value or "00:00:00"), "%H:%M:%S").time()


def _scheduled_wallclock(now: datetime, hms: str) -> datetime:
    return datetime.combine(now.date(), _parse_time(hms), now.tzinfo)


def _parse_iso_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _kill_pid_tree(pid: int) -> None:
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            return
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except Exception:
        return


def _phase_forced_deadline(config: Dict[str, Any], phase_name: str, now: datetime, started_at: datetime | None) -> datetime | None:
    if started_at is None:
        return None
    specs = _phase_specs(config)
    phase_spec = specs.get(phase_name)
    if phase_spec is None:
        return None
    timeout_deadline = started_at + timedelta(minutes=max(int(phase_spec.timeout_minutes or 0), 1))
    phase_cutoff_map = {
        "research_refresh": "release_refresh",
        "release_refresh": "preopen_gate",
        "preopen_gate": "simulation",
    }
    cutoff_phase = phase_cutoff_map.get(phase_name, "")
    if not cutoff_phase:
        return timeout_deadline
    cutoff_spec = specs.get(cutoff_phase)
    if cutoff_spec is None:
        return timeout_deadline
    cutoff_deadline = _scheduled_wallclock(now, cutoff_spec.scheduled_time)
    return min(timeout_deadline, cutoff_deadline)


def _phase_can_degrade_to_skipped(config: Dict[str, Any], trade_date: str, phase_name: str) -> bool:
    if phase_name != "research_refresh":
        return False
    latest_release = _latest_release_for_trade_date(config, trade_date)
    return bool(str(latest_release.get("release_id", "") or "").strip())


def _reconcile_running_phases(config: Dict[str, Any], profile: str, now: datetime) -> bool:
    mutated = False
    candidate_dates = []
    current_trade_date = _current_trade_date(config, now)
    next_trade_date = _next_trade_date(config, now.date())
    for value in [current_trade_date, next_trade_date]:
        text = str(value or "").strip()
        if text and text not in candidate_dates:
            candidate_dates.append(text)
    for trade_date in candidate_dates:
        state = _ensure_cycle_state(config, trade_date, profile)
        current_phase = str(state.get("current_phase", "") or "").strip()
        if not current_phase:
            continue
        phase_entry = dict(state.get("phases", {}).get(current_phase, {}) or {})
        if str(phase_entry.get("status", "") or "").strip() != "running":
            continue
        phase_spec = _phase_specs(config).get(current_phase)
        lock_name = phase_spec.lock_name if phase_spec is not None else ""
        lock_payload = _load_json(_lock_path(config, lock_name), default={}) if lock_name else {}
        owner_pid = int(lock_payload.get("owner_pid", 0) or 0)
        child_pid = int(lock_payload.get("child_pid", 0) or 0)
        if (not _process_alive(child_pid)) and (not _process_alive(owner_pid)):
            reset_entry = dict(phase_entry)
            reset_entry.update(
                {
                    "status": "queued",
                    "started_at": "",
                    "finished_at": "",
                    "return_code": None,
                    "error_message": "orphaned_running_phase_requeued",
                    "result_status": "",
                    "stdout_tail": [],
                    "stderr_tail": [],
                }
            )
            state["phases"][current_phase] = reset_entry
            state["current_phase"] = ""
            _save_cycle_state(config, state)
            mutated = True
            continue
        started_at = _parse_iso_datetime(str(phase_entry.get("started_at", "") or ""))
        forced_deadline = _phase_forced_deadline(config, current_phase, now, started_at)
        if forced_deadline is None or now < forced_deadline:
            continue
        if lock_name:
            if _process_alive(child_pid):
                _kill_pid_tree(child_pid)
        result_status = "forced_deadline_skip" if _phase_can_degrade_to_skipped(config, trade_date, current_phase) else "forced_deadline_timeout"
        phase_status = "skipped" if result_status == "forced_deadline_skip" else "timeout"
        reason = f"{result_status}:{forced_deadline.isoformat(timespec='seconds')}"
        _mark_phase_complete(
            config=config,
            trade_date=trade_date,
            profile=profile,
            phase_name=current_phase,
            phase_result={
                "status": phase_status,
                "return_code": None,
                "release_id": str(state.get("release_id", "") or ""),
                "warning_count": 1,
                "error_message": reason,
                "stdout_log": str(phase_entry.get("stdout_log", "") or ""),
                "stderr_log": str(phase_entry.get("stderr_log", "") or ""),
                "stdout_tail": list(phase_entry.get("stdout_tail", []) or []),
                "stderr_tail": list(phase_entry.get("stderr_tail", []) or []) + [reason],
                "result_status": result_status,
                "result_payload": dict(phase_entry.get("result_payload", {}) or {}),
            },
        )
        mutated = True
    return mutated


def _next_trade_date(config: Dict[str, Any], base_date: date) -> str:
    next_info = next_trading_day(config=config, base_date=base_date, include_today=False)
    return str(next_info.get("next_trading_day", "") or "")


def _current_trade_date(config: Dict[str, Any], now: datetime) -> str:
    trading_day = is_trading_day(config=config, target_date=now.date())
    if bool(trading_day.get("ok", False)) and bool(trading_day.get("is_trading_day", False)):
        return now.date().isoformat()
    return ""


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    return lines[-max(int(limit or 0), 0):]


def _count_warning_lines(lines: Iterable[str]) -> int:
    count = 0
    for line in lines:
        lowered = str(line or "").lower()
        if "warning" in lowered or "warn" in lowered or "风险" in lowered:
            count += 1
    return count


def _extract_result_json(stdout_text: str) -> Dict[str, Any]:
    if RESULT_START not in stdout_text or RESULT_END not in stdout_text:
        return {}
    raw = stdout_text.split(RESULT_START, 1)[1].split(RESULT_END, 1)[0].strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _subprocess_phase(
    config: Dict[str, Any],
    trade_date: str,
    phase_name: str,
    command: list[str],
    timeout_minutes: int,
) -> Dict[str, Any]:
    logs = _runtime_log_paths(config, trade_date, phase_name)
    lock = _acquire_lock(config, _phase_specs(config)[phase_name].lock_name, trade_date=trade_date, phase_name=phase_name)
    if lock is None:
        return {
            "ok": False,
            "phase_status": "skipped",
            "return_code": None,
            "error_message": "lock_held",
            "stdout_log": str(logs["stdout"]),
            "stderr_log": str(logs["stderr"]),
            "stdout_tail": [],
            "stderr_tail": [],
            "result_payload": {},
            "warning_count": 0,
        }
    if should_delegate_phase(config, phase_name):
        remote_result = run_remote_phase(
            config=config,
            phase_name=phase_name,
            command=command,
            local_repo_root=_repo_root(),
            stdout_log=logs["stdout"],
            stderr_log=logs["stderr"],
            timeout_minutes=timeout_minutes,
        )
        if bool(remote_result.get("delegated", False)) and (bool(remote_result.get("ok", False)) or not bool(remote_result.get("fallback_to_local", True))):
            stdout_tail = _tail_lines(logs["stdout"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
            stderr_tail = _tail_lines(logs["stderr"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
            stdout_text = logs["stdout"].read_text(encoding="utf-8", errors="ignore") if logs["stdout"].exists() else ""
            result_payload = _extract_result_json(stdout_text)
            result_payload["remote_delegate"] = {
                "applied": True,
                "remote_target": str(remote_result.get("remote_target", "") or ""),
                "phase_name": phase_name,
            }
            _release_lock(config, _phase_specs(config)[phase_name].lock_name)
            return {
                "ok": bool(remote_result.get("ok", False)),
                "timed_out": str(remote_result.get("error", "") or "") == "remote_phase_timeout",
                "return_code": remote_result.get("return_code"),
                "error_message": "" if bool(remote_result.get("ok", False)) else str(remote_result.get("error", "") or f"remote_delegate_exit_{remote_result.get('return_code')}"),
                "stdout_log": str(logs["stdout"]),
                "stderr_log": str(logs["stderr"]),
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "result_payload": result_payload,
                "warning_count": _count_warning_lines(list(stdout_tail) + list(stderr_tail)),
            }
    timed_out = False
    process: subprocess.Popen[str] | None = None
    try:
        with logs["stdout"].open("w", encoding="utf-8") as stdout_handle, logs["stderr"].open("w", encoding="utf-8") as stderr_handle:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(_repo_root()),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )
            except OSError as exc:
                stderr_handle.write(f"spawn_failed: {exc}\n")
                stderr_handle.flush()
                return {
                    "ok": False,
                    "timed_out": False,
                    "return_code": None,
                    "error_message": f"spawn_failed: {exc}",
                    "stdout_log": str(logs["stdout"]),
                    "stderr_log": str(logs["stderr"]),
                    "stdout_tail": [],
                    "stderr_tail": [f"spawn_failed: {exc}"],
                    "result_payload": {},
                    "warning_count": 0,
                }
            _update_lock_child_pid(config, _phase_specs(config)[phase_name].lock_name, process.pid)
            deadline = time.time() + max(int(timeout_minutes or 0), 1) * 60
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                if time.time() >= deadline:
                    timed_out = True
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    break
                time.sleep(5)
        stdout_tail = _tail_lines(logs["stdout"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
        stderr_tail = _tail_lines(logs["stderr"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
        stdout_text = logs["stdout"].read_text(encoding="utf-8", errors="ignore") if logs["stdout"].exists() else ""
        result_payload = _extract_result_json(stdout_text)
        return_code = process.returncode if process is not None else None
        error_message = ""
        if timed_out:
            error_message = f"timeout_after_{int(timeout_minutes or 0)}m"
        elif return_code not in (0, None):
            error_message = "\n".join(stderr_tail[-3:]).strip() or f"child_exit_{return_code}"
        elif isinstance(result_payload, dict) and str(result_payload.get("error", "") or "").strip():
            error_message = str(result_payload.get("error", "") or "").strip()
        return {
            "ok": (not timed_out) and return_code == 0,
            "timed_out": timed_out,
            "return_code": return_code,
            "error_message": error_message,
            "stdout_log": str(logs["stdout"]),
            "stderr_log": str(logs["stderr"]),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "result_payload": result_payload,
            "warning_count": _count_warning_lines(list(stdout_tail) + list(stderr_tail)),
        }
    finally:
        _release_lock(config, _phase_specs(config)[phase_name].lock_name)


def _affordable_bundle_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("affordable_data_bundle", {}) or {})


def _research_fact_refresh_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("research_fact_refresh", {}) or {})


def _external_research_refresh_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("external_research_refresh", {}) or {})


def _derived_alpha_refresh_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("derived_alpha_refresh", {}) or {})


def _audit_site_publish_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("audit_site_publish", {}) or {})


def _operator_runtime_publish_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("operator_runtime_publish", {}) or {})


def _intraday_state_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("intraday_state_machine", {}) or {})


def _intraday_state_root(config: Dict[str, Any]) -> Path:
    cfg = _intraday_state_cfg(config)
    default_root = _trade_clock_root(config) / "intraday_state"
    return ensure_dir(Path(str(cfg.get("artifact_root", default_root) or default_root)).resolve())


def _latest_intraday_manifest_path(config: Dict[str, Any]) -> Path:
    return _intraday_state_root(config) / "latest" / "intraday_state_manifest.json"


def _latest_intraday_control_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    return _load_json(_intraday_state_root(config) / "latest" / "intraday_control_summary.json", default={})


def _latest_t_audit_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    data_root = Path(str(config.get("paths", {}).get("data_root", _repo_root() / "data") or _repo_root() / "data")).resolve()
    audit_cfg = dict(config.get("t_audit", {}) or {})
    root = Path(str(audit_cfg.get("artifact_root", data_root / "audit_v1") or data_root / "audit_v1")).resolve()
    return _load_json(root / "latest" / "latest_t_audit.json", default={})


def _run_intraday_state_refresh(
    config: Dict[str, Any],
    trade_date: str,
    source_phase: str,
    cycle_state: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = _intraday_state_cfg(config)
    if not bool(cfg.get("enabled", True)):
        return {"ran": False, "ok": True, "message": "intraday_state_machine_disabled"}
    refresh_phases = {
        str(item).strip()
        for item in list(
            cfg.get(
                "refresh_on_phase_completion",
                ["preopen_gate", "simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow", "summary"],
            )
            or []
        )
        if str(item).strip()
    }
    sp = str(source_phase or "").strip()
    if refresh_phases and sp not in refresh_phases and not sp.startswith("intraday_tactical_"):
        return {"ran": False, "ok": True, "message": "phase_not_selected"}
    return refresh_intraday_state_machine(
        config=config,
        trade_date=str(trade_date or ""),
        source_phase=str(source_phase or ""),
        cycle_state=cycle_state,
    )


def _apply_intraday_afternoon_overlay(
    config: Dict[str, Any],
    phase_name: str,
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    if phase_name not in {"afternoon_execution", "afternoon_shadow"}:
        return dict(plan or {})
    cfg = _intraday_state_cfg(config)
    # shadow_mode 只影响日内状态机 JSON 里的 integration 标签；默认仍要把 overlay 合并进下午执行计划。
    # 若需恢复旧行为（shadow 下完全忽略 overlay），设 INTRADAY_AFTERNOON_OVERLAY_RESPECT_SHADOW_MODE=True。
    respect_shadow = bool(cfg.get("afternoon_overlay_respect_shadow_mode", False))
    if (
        not bool(cfg.get("enabled", True))
        or not bool(cfg.get("enable_afternoon_overlay", True))
    ):
        return dict(plan or {})
    if respect_shadow and bool(cfg.get("shadow_mode", True)):
        return dict(plan or {})
    summary = _latest_intraday_control_summary(config)
    overlay = dict(summary.get("overlay_recommendation", {}) or {})
    if not overlay:
        return dict(plan or {})
    updated = dict(plan or {})
    midday_action = str(summary.get("midday_action", "") or "")
    updated["intraday_overlay"] = overlay
    updated["midday_action"] = midday_action
    updated["timing_window"] = str(summary.get("timing_window", "") or overlay.get("timing_window", "") or "")
    updated["projected_afternoon_window"] = str(
        summary.get("projected_afternoon_window", "") or overlay.get("projected_afternoon_window", "") or ""
    )
    updated["timing_layer_active"] = bool(overlay.get("timing_layer_active", False))
    updated["buy_ready_count"] = int(overlay.get("buy_ready_count", 0) or 0)
    updated["sell_ready_count"] = int(overlay.get("sell_ready_count", 0) or 0)
    updated["afternoon_second_leg_candidates_count"] = int(
        overlay.get("afternoon_second_leg_candidates_count", 0) or 0
    )
    updated["t_triggered_count"] = int(overlay.get("t_triggered_count", 0) or 0)
    updated["block_new_t"] = bool(overlay.get("block_new_t", False))
    t_audit = _latest_t_audit_summary(config)
    if bool(t_audit.get("available", False)):
        updated["t_audit_top_reject_reason"] = str(t_audit.get("top_reject_reason", "") or "")
        updated["t_audit_top_suited_mechanism"] = str(t_audit.get("top_suited_mechanism", "") or "")
        updated["t_audit_policy_change_suggestions"] = list(t_audit.get("policy_change_suggestions", []) or [])[:3]
        top_reject_reason = str(t_audit.get("top_reject_reason", "") or "")
        if top_reject_reason in {"system_halt", "snapshot_degraded", "quality_below_minimum"}:
            updated["ignore_market_panic_reduce_only"] = False
            updated["allow_unfinished_orders_reconcile"] = True
            if int(updated.get("t_triggered_count", 0) or 0) <= 0 and int(updated.get("buy_ready_count", 0) or 0) <= 0:
                updated["should_run"] = bool(updated.get("allow_unfinished_orders_reconcile", False))
                updated["reason"] = str(updated.get("reason", "") or f"t_audit_{top_reject_reason}")
        top_suited_mechanism = str(t_audit.get("top_suited_mechanism", "") or "")
        if top_suited_mechanism and top_suited_mechanism not in {"unknown", "unlabeled"}:
            updated["preferred_t_mechanism"] = top_suited_mechanism
    if bool(overlay.get("allow_unfinished_orders_reconcile", False)):
        updated["allow_unfinished_orders_reconcile"] = True
    if bool(overlay.get("block_new_entries", False)):
        updated["ignore_market_panic_reduce_only"] = False
        if midday_action == "abort_new_entries" and int(dict(summary.get("risk_summary", {}) or {}).get("open_intents_after", 0) or 0) <= 0:
            updated["should_run"] = False
            updated["reason"] = str(updated.get("reason", "") or "intraday_abort_new_entries")
    if bool(overlay.get("force_reconcile_only", False)):
        updated["allow_unfinished_orders_reconcile"] = True
        updated["should_run"] = True
        updated["reason"] = str(updated.get("reason", "") or "intraday_force_reconcile_only")
    return updated


def _auxiliary_runtime_log_paths(config: Dict[str, Any], trade_date: str, name: str) -> Dict[str, Path]:
    root = _phase_runtime_dir(config, trade_date)
    return {
        "root": root,
        "stdout": root / f"{name}.stdout.log",
        "stderr": root / f"{name}.stderr.log",
    }


def _subprocess_auxiliary(
    config: Dict[str, Any],
    trade_date: str,
    name: str,
    command: list[str],
    timeout_minutes: int,
) -> Dict[str, Any]:
    logs = _auxiliary_runtime_log_paths(config, trade_date, name)
    timed_out = False
    process: subprocess.Popen[str] | None = None
    with logs["stdout"].open("w", encoding="utf-8") as stdout_handle, logs["stderr"].open("w", encoding="utf-8") as stderr_handle:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(_repo_root()),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        except OSError as exc:
            stderr_handle.write(f"spawn_failed: {exc}\n")
            stderr_handle.flush()
            return {
                "ok": False,
                "timed_out": False,
                "return_code": None,
                "error_message": f"spawn_failed: {exc}",
                "stdout_log": str(logs["stdout"]),
                "stderr_log": str(logs["stderr"]),
                "stdout_tail": [],
                "stderr_tail": [f"spawn_failed: {exc}"],
                "result_payload": {},
                "warning_count": 0,
            }
        deadline = time.time() + max(int(timeout_minutes or 0), 1) * 60
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            if time.time() >= deadline:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                break
            time.sleep(5)
    stdout_tail = _tail_lines(logs["stdout"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
    stderr_tail = _tail_lines(logs["stderr"], int(_scheduler_cfg(config).get("log_tail_lines", 30) or 30))
    stdout_text = logs["stdout"].read_text(encoding="utf-8", errors="ignore") if logs["stdout"].exists() else ""
    return_code = process.returncode if process is not None else None
    payload: Dict[str, Any] = {}
    try:
        payload = _extract_result_json(stdout_text)
    except Exception:
        payload = {}
    error_message = ""
    if timed_out:
        error_message = f"timeout_after_{int(timeout_minutes or 0)}m"
    elif return_code not in (0, None):
        error_message = "\n".join(stderr_tail[-3:]).strip() or f"child_exit_{return_code}"
    return {
        "ok": (not timed_out) and return_code == 0,
        "timed_out": timed_out,
        "return_code": return_code,
        "error_message": error_message,
        "stdout_log": str(logs["stdout"]),
        "stderr_log": str(logs["stderr"]),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "result_payload": payload,
        "warning_count": _count_warning_lines(list(stdout_tail) + list(stderr_tail)),
    }


def _run_affordable_data_refresh(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    bundle_cfg = _affordable_bundle_cfg(config)
    if not bool(bundle_cfg.get("enabled", True)) or not bool(bundle_cfg.get("run_before_research", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    script_path = Path(str(bundle_cfg.get("script_path", "") or "")).resolve()
    if not script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{script_path}"}
    command = [
        _research_python_from_config(config),
        str(script_path),
        "--db-path",
        str(Path(str(bundle_cfg.get("sqlite_path", "") or "")).resolve()),
        "--snapshot-root",
        str(Path(str(bundle_cfg.get("snapshot_root", "") or "")).resolve()),
        "--daily-lookback",
        str(int(bundle_cfg.get("daily_lookback", 3) or 3)),
        "--announcement-lookback",
        str(int(bundle_cfg.get("announcement_lookback", 30) or 30)),
    ]
    for dataset in list(bundle_cfg.get("datasets", []) or []):
        text = str(dataset or "").strip()
        if text:
            command.extend(["--dataset", text])
    raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="affordable_data_refresh",
        command=command,
        timeout_minutes=int(bundle_cfg.get("timeout_minutes", 120) or 120),
    )
    return {
        "enabled": True,
        "ran": True,
        "ok": bool(raw.get("ok", False)),
        "fail_open": bool(bundle_cfg.get("fail_open", True)),
        "message": str(raw.get("error_message", "") or ""),
        "stdout_log": str(raw.get("stdout_log", "") or ""),
        "stderr_log": str(raw.get("stderr_log", "") or ""),
        "warning_count": int(raw.get("warning_count", 0) or 0),
        "result_payload": dict(raw.get("result_payload", {}) or {}),
    }


def _run_research_fact_refresh(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    refresh_cfg = _research_fact_refresh_cfg(config)
    if not bool(refresh_cfg.get("enabled", True)) or not bool(refresh_cfg.get("run_before_research", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    event_script_path = Path(str(refresh_cfg.get("event_script_path", "") or "")).resolve()
    hard_factor_script_path = Path(str(refresh_cfg.get("hard_factor_script_path", "") or "")).resolve()
    sqlite_path = Path(str(refresh_cfg.get("sqlite_path", "") or "")).resolve()
    if not event_script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{event_script_path}"}
    if not hard_factor_script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{hard_factor_script_path}"}
    timeout_minutes = int(refresh_cfg.get("timeout_minutes", 90) or 90)
    event_raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="research_fact_event_refresh",
        command=[
            _research_python_from_config(config),
            str(event_script_path),
            "--db-path",
            str(sqlite_path),
            "--lookback-days",
            str(int(refresh_cfg.get("event_lookback_days", 60) or 60)),
        ],
        timeout_minutes=max(15, timeout_minutes // 2),
    )
    hard_factor_raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="research_fact_hard_factor_refresh",
        command=[
            _research_python_from_config(config),
            str(hard_factor_script_path),
            "--db-path",
            str(sqlite_path),
            "--lookback-days",
            str(int(refresh_cfg.get("hard_factor_lookback_days", 5) or 5)),
        ],
        timeout_minutes=max(15, timeout_minutes // 2),
    )
    ok = bool(event_raw.get("ok", False)) and bool(hard_factor_raw.get("ok", False))
    messages = [
        str(event_raw.get("error_message", "") or "").strip(),
        str(hard_factor_raw.get("error_message", "") or "").strip(),
    ]
    return {
        "enabled": True,
        "ran": True,
        "ok": ok,
        "fail_open": bool(refresh_cfg.get("fail_open", True)),
        "message": "; ".join(text for text in messages if text),
        "warning_count": int(event_raw.get("warning_count", 0) or 0) + int(hard_factor_raw.get("warning_count", 0) or 0),
        "result_payload": {
            "sqlite_path": str(sqlite_path),
            "event_refresh": {
                "ok": bool(event_raw.get("ok", False)),
                "stdout_log": str(event_raw.get("stdout_log", "") or ""),
                "stderr_log": str(event_raw.get("stderr_log", "") or ""),
                "result_payload": dict(event_raw.get("result_payload", {}) or {}),
            },
            "hard_factor_refresh": {
                "ok": bool(hard_factor_raw.get("ok", False)),
                "stdout_log": str(hard_factor_raw.get("stdout_log", "") or ""),
                "stderr_log": str(hard_factor_raw.get("stderr_log", "") or ""),
                "result_payload": dict(hard_factor_raw.get("result_payload", {}) or {}),
            },
        },
    }


def _run_external_research_refresh(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    refresh_cfg = _external_research_refresh_cfg(config)
    if not bool(refresh_cfg.get("enabled", True)) or not bool(refresh_cfg.get("run_before_research", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    script_path = Path(str(refresh_cfg.get("script_path", "") or "")).resolve()
    sqlite_path = Path(str(refresh_cfg.get("sqlite_path", "") or "")).resolve()
    if not script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{script_path}"}
    raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="external_research_refresh",
        command=[
            _research_python_from_config(config),
            str(script_path),
            "--db-path",
            str(sqlite_path),
        ],
        timeout_minutes=int(refresh_cfg.get("timeout_minutes", 45) or 45),
    )
    return {
        "enabled": True,
        "ran": True,
        "ok": bool(raw.get("ok", False)),
        "fail_open": bool(refresh_cfg.get("fail_open", True)),
        "message": str(raw.get("error_message", "") or ""),
        "stdout_log": str(raw.get("stdout_log", "") or ""),
        "stderr_log": str(raw.get("stderr_log", "") or ""),
        "warning_count": int(raw.get("warning_count", 0) or 0),
        "result_payload": dict(raw.get("result_payload", {}) or {}),
    }


def _run_market_pipeline_refresh(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    market_cfg = dict(config.get("market_pipeline", {}) or {})
    if not bool(market_cfg.get("enabled", False)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    if not bool(market_cfg.get("run_before_research_gate", True)):
        return {"enabled": True, "ran": False, "ok": True, "message": "disabled_by_run_before_research_gate"}
    try:
        payload = run_market_pipeline(config=config)
    except Exception as exc:
        return {
            "enabled": True,
            "ran": True,
            "ok": False,
            "fail_open": bool(market_cfg.get("fail_open", False)),
            "message": str(exc),
            "trade_date": str(trade_date or ""),
            "result_payload": {},
        }
    errors = {
        str(key): str(value)
        for key, value in dict(payload or {}).items()
        if str(key).endswith("_error") and str(value or "").strip()
    }
    return {
        "enabled": True,
        "ran": True,
        "ok": not bool(errors),
        "fail_open": bool(market_cfg.get("fail_open", False)),
        "message": "ok" if not errors else "; ".join(f"{key}={value}" for key, value in errors.items()),
        "trade_date": str(trade_date or ""),
        "result_payload": dict(payload or {}),
    }


def _run_derived_alpha_refresh(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    refresh_cfg = _derived_alpha_refresh_cfg(config)
    if not bool(refresh_cfg.get("enabled", True)) or not bool(refresh_cfg.get("run_before_research", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    try:
        payload = run_derived_alpha_refresh(config=config)
    except Exception as exc:
        return {
            "enabled": True,
            "ran": True,
            "ok": False,
            "fail_open": bool(refresh_cfg.get("fail_open", False)),
            "message": str(exc),
            "trade_date": str(trade_date or ""),
            "result_payload": {},
            "warning_count": 1,
        }
    return {
        "enabled": True,
        "ran": True,
        "ok": bool(payload.get("ok", False)),
        "fail_open": bool(refresh_cfg.get("fail_open", False)),
        "message": str(payload.get("message", "") or ""),
        "trade_date": str(trade_date or ""),
        "result_payload": dict(payload or {}),
        "warning_count": 0 if bool(payload.get("ok", False)) else 1,
    }


def run_pre_research_refresh_bundle(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    affordable_refresh = _run_affordable_data_refresh(config=config, trade_date=trade_date)
    external_research_refresh = _run_external_research_refresh(config=config, trade_date=trade_date)
    research_fact_refresh = _run_research_fact_refresh(config=config, trade_date=trade_date)
    market_pipeline_refresh = _run_market_pipeline_refresh(config=config, trade_date=trade_date)
    derived_alpha_refresh = _run_derived_alpha_refresh(config=config, trade_date=trade_date)
    entries = [
        ("affordable_data_refresh", affordable_refresh),
        ("external_research_refresh", external_research_refresh),
        ("research_fact_refresh", research_fact_refresh),
        ("market_pipeline_refresh", market_pipeline_refresh),
        ("derived_alpha_refresh", derived_alpha_refresh),
    ]
    blocking_failure: Dict[str, Any] = {}
    warning_count = 0
    failed_but_open: list[str] = []
    for name, payload in entries:
        warning_count += int(payload.get("warning_count", 0) or 0)
        if bool(payload.get("ran", False)) and not bool(payload.get("ok", False)):
            warning_count += 1
            if not bool(payload.get("fail_open", True)):
                blocking_failure = {
                    "name": name,
                    "message": str(payload.get("message", "") or f"{name}_failed"),
                    "payload": dict(payload or {}),
                }
                break
            failed_but_open.append(name)
    return {
        "ok": not bool(blocking_failure),
        "warning_count": int(warning_count),
        "failed_but_open": failed_but_open,
        "blocking_failure": blocking_failure,
        "affordable_data_refresh": affordable_refresh,
        "external_research_refresh": external_research_refresh,
        "research_fact_refresh": research_fact_refresh,
        "market_pipeline_refresh": market_pipeline_refresh,
        "derived_alpha_refresh": derived_alpha_refresh,
    }


def _run_live_snapshot_refresh(config: Dict[str, Any], trade_date: str, phase_name: str, *, refresh_mode: str = "phase") -> Dict[str, Any]:
    scheduler = _scheduler_cfg(config)
    if not bool(scheduler.get("live_snapshot_refresh_enabled", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    refresh_phases = {
        str(item or "").strip()
        for item in list(scheduler.get("live_snapshot_refresh_phases", []) or [])
        if str(item or "").strip()
    }
    if refresh_phases and str(phase_name or "").strip() not in refresh_phases:
        return {"enabled": True, "ran": False, "ok": True, "message": "phase_not_selected"}
    try:
        client = TushareClient(dict(config.get("providers", {}).get("tushare", {}) or {}))
        price_snapshot = build_daily_price_snapshot(config=config, client=client)
        intraday_proxy = build_intraday_proxy_snapshot(config=config, client=client, refresh_mode=refresh_mode)
        rows = int(price_snapshot.get("rows", 0) or 0)
        ok_rows = bool(
            rows > 0
            or int(intraday_proxy.get("quote_rows", 0) or 0) > 0
            or int(intraday_proxy.get("list_rows", 0) or 0) > 0
            or int(intraday_proxy.get("rt_min_rows", 0) or 0) > 0
        )
        return {
            "enabled": True,
            "ran": True,
            "ok": ok_rows,
            "fail_open": bool(scheduler.get("live_snapshot_refresh_fail_open", True)),
            "message": "ok" if ok_rows else "empty_snapshot",
            "phase_name": str(phase_name or ""),
            "trade_date": str(trade_date or ""),
            "refresh_mode": str(refresh_mode or "phase"),
            "result_payload": {
                "daily_price_snapshot": price_snapshot,
                "intraday_proxy": intraday_proxy,
            },
        }
    except Exception as exc:
        return {
            "enabled": True,
            "ran": True,
            "ok": False,
            "fail_open": bool(scheduler.get("live_snapshot_refresh_fail_open", True)),
            "message": str(exc),
            "phase_name": str(phase_name or ""),
            "trade_date": str(trade_date or ""),
            "refresh_mode": str(refresh_mode or "phase"),
            "result_payload": {},
        }


def _run_audit_site_publish(config: Dict[str, Any], trade_date: str, report_dir: Path) -> Dict[str, Any]:
    publish_cfg = _audit_site_publish_cfg(config)
    if not bool(publish_cfg.get("enabled", True)) or not bool(publish_cfg.get("run_after_summary", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    script_path = Path(str(publish_cfg.get("script_path", "") or "")).resolve()
    if not script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{script_path}"}
    if not report_dir.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_report_dir:{report_dir}"}
    powershell_exe = str(publish_cfg.get("powershell_executable", "powershell.exe") or "powershell.exe").strip() or "powershell.exe"
    command = [
        powershell_exe,
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-PythonExe",
        str(publish_cfg.get("python_executable", _research_python_from_config(config)) or _research_python_from_config(config)),
        "-ReportDir",
        str(report_dir.resolve()),
        "-RemoteUser",
        str(publish_cfg.get("remote_user", "ubuntu") or "ubuntu"),
        "-RemoteHost",
        str(publish_cfg.get("remote_host", "43.129.28.141") or "43.129.28.141"),
        "-RemoteRoot",
        str(publish_cfg.get("remote_root", "/var/www/peng1145141919810.xyz/site") or "/var/www/peng1145141919810.xyz/site"),
        "-Domain",
        str(publish_cfg.get("domain", "peng1145141919810.xyz") or "peng1145141919810.xyz"),
    ]
    raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="audit_site_publish",
        command=command,
        timeout_minutes=int(publish_cfg.get("timeout_minutes", 20) or 20),
    )
    return {
        "enabled": True,
        "ran": True,
        "ok": bool(raw.get("ok", False)),
        "fail_open": bool(publish_cfg.get("fail_open", True)),
        "message": str(raw.get("error_message", "") or ""),
        "stdout_log": str(raw.get("stdout_log", "") or ""),
        "stderr_log": str(raw.get("stderr_log", "") or ""),
        "warning_count": int(raw.get("warning_count", 0) or 0),
        "result_payload": dict(raw.get("result_payload", {}) or {}),
    }


def _run_operator_runtime_publish(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    publish_cfg = _operator_runtime_publish_cfg(config)
    if not bool(publish_cfg.get("enabled", True)):
        return {"enabled": False, "ran": False, "ok": True, "message": "disabled"}
    script_path = Path(str(publish_cfg.get("script_path", "") or "")).resolve()
    if not script_path.exists():
        return {"enabled": True, "ran": False, "ok": False, "message": f"missing_script:{script_path}"}
    powershell_exe = str(publish_cfg.get("powershell_executable", "powershell.exe") or "powershell.exe").strip() or "powershell.exe"
    command = [
        powershell_exe,
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-PythonExe",
        str(publish_cfg.get("python_executable", _research_python_from_config(config)) or _research_python_from_config(config)),
        "-RemoteUser",
        str(publish_cfg.get("remote_user", "ubuntu") or "ubuntu"),
        "-RemoteHost",
        str(publish_cfg.get("remote_host", "43.129.28.141") or "43.129.28.141"),
        "-RemoteRoot",
        str(publish_cfg.get("remote_root", "/var/www/peng1145141919810.xyz/site") or "/var/www/peng1145141919810.xyz/site"),
    ]
    raw = _subprocess_auxiliary(
        config=config,
        trade_date=trade_date,
        name="operator_runtime_publish",
        command=command,
        timeout_minutes=int(publish_cfg.get("timeout_minutes", 5) or 5),
    )
    return {
        "enabled": True,
        "ran": True,
        "ok": bool(raw.get("ok", False)),
        "fail_open": bool(publish_cfg.get("fail_open", True)),
        "message": str(raw.get("error_message", "") or ""),
        "stdout_log": str(raw.get("stdout_log", "") or ""),
        "stderr_log": str(raw.get("stderr_log", "") or ""),
        "warning_count": int(raw.get("warning_count", 0) or 0),
        "result_payload": dict(raw.get("result_payload", {}) or {}),
    }

def _latest_release_safe(config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return load_latest_release(config)
    except Exception:
        return {}


def _release_by_id_safe(config: Dict[str, Any], release_id: str) -> Dict[str, Any]:
    if not str(release_id or "").strip():
        return {}
    try:
        return load_release_by_id(config, release_id=release_id)
    except Exception:
        return {}


def _portfolio_artifact_freshness_hours(summary_path: Path, target_path: Path) -> float:
    latest_ts = max(summary_path.stat().st_mtime, target_path.stat().st_mtime)
    age_seconds = max(time.time() - latest_ts, 0.0)
    return age_seconds / 3600.0


def _infer_summary_trade_date(summary_doc: Dict[str, Any]) -> str:
    explicit_trade_date = str(summary_doc.get("trade_date", "") or "").strip()
    if explicit_trade_date:
        return explicit_trade_date
    generated_at = str(summary_doc.get("generated_at", "") or "").strip()
    if generated_at:
        normalized = generated_at.replace("T", " ")
        return normalized[:10]
    return ""


def _find_fallback_source(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    scheduler = _scheduler_cfg(config)
    max_age_hours = float(scheduler.get("fallback_max_portfolio_age_hours", 18) or 18)
    portfolio_root = Path(str(config.get("paths", {}).get("portfolio_output_root", "") or "")).resolve()
    summary_path = portfolio_root / "portfolio_recommendation.json"
    target_path = portfolio_root / "target_positions.csv"
    if summary_path.exists() and target_path.exists():
        age_hours = _portfolio_artifact_freshness_hours(summary_path, target_path)
        summary_doc = _load_json(summary_path, default={})
        summary_trade_date = _infer_summary_trade_date(summary_doc)
        if age_hours <= max_age_hours and summary_trade_date == str(trade_date):
            return {
                "ok": True,
                "kind": "portfolio_artifacts",
                "summary_path": str(summary_path),
                "target_positions_path": str(target_path),
                "age_hours": round(age_hours, 3),
                "summary_trade_date": summary_trade_date,
                "fallback_source_release_id": "",
                "fallback_reason": "research_phase_failed_use_recent_portfolio_artifacts",
            }
    latest_release = _latest_release_safe(config)
    artifacts = dict(latest_release.get("artifacts", {}) or {})
    summary_path = Path(str(artifacts.get("portfolio_summary_path", "") or "")).resolve() if str(artifacts.get("portfolio_summary_path", "") or "").strip() else Path()
    target_path = Path(str(artifacts.get("target_positions_path", "") or "")).resolve() if str(artifacts.get("target_positions_path", "") or "").strip() else Path()
    if summary_path.exists() and target_path.exists():
        age_hours = _portfolio_artifact_freshness_hours(summary_path, target_path)
        latest_release_trade_date = str(latest_release.get("trade_date", "") or "")
        if age_hours <= max_age_hours and latest_release_trade_date == str(trade_date):
            return {
                "ok": True,
                "kind": "latest_release_artifacts",
                "summary_path": str(summary_path),
                "target_positions_path": str(target_path),
                "age_hours": round(age_hours, 3),
                "summary_trade_date": latest_release_trade_date,
                "fallback_source_release_id": str(latest_release.get("release_id", "") or ""),
                "fallback_reason": "research_phase_failed_use_latest_release_artifacts",
            }
    return {
        "ok": False,
        "kind": "",
        "summary_path": "",
        "target_positions_path": "",
        "age_hours": None,
        "summary_trade_date": "",
        "fallback_source_release_id": str(latest_release.get("release_id", "") or ""),
        "fallback_reason": "no_acceptable_fallback_source",
    }


def _midday_plan_payload(cycle_state: Dict[str, Any]) -> Dict[str, Any]:
    return dict(dict(cycle_state.get("phases", {}).get("midday_review", {}) or {}).get("result_payload", {}) or {})


def _phase_execution_plan(config: Dict[str, Any], cycle_state: Dict[str, Any], phase_name: str) -> Dict[str, Any]:
    midday_plan = _midday_plan_payload(cycle_state)
    if phase_name == "afternoon_execution":
        return _apply_intraday_afternoon_overlay(config=config, phase_name=phase_name, plan=dict(midday_plan.get("real_execution", {}) or {}))
    if phase_name == "afternoon_shadow":
        return _apply_intraday_afternoon_overlay(config=config, phase_name=phase_name, plan=dict(midday_plan.get("shadow_execution", {}) or {}))
    return {}


def _phase_command(
    config: Dict[str, Any],
    phase_name: str,
    profile: str,
    trade_date: str,
    cycle_state: Dict[str, Any],
) -> list[str]:
    scheduler = _scheduler_cfg(config)
    command_profile = str(profile or "")
    if phase_name == "research_refresh":
        command_profile = str(scheduler.get("research_refresh_profile", "quick_test") or "quick_test")
    elif phase_name == "release_refresh":
        command_profile = str(scheduler.get("release_refresh_profile", command_profile) or command_profile)
    command = [sys.executable, str(_repo_root() / "launch_canonical.py"), "--profile", command_profile, "--skip-preflight"]
    if phase_name == "research":
        return command + ["--mode", "research_only"]
    if phase_name == "release":
        fallback = dict(cycle_state.get("fallback", {}) or {})
        release_source_mode = "release_only"
        release_note = ""
        extra: list[str] = []
        if bool(fallback.get("active", False)):
            release_source_mode = "fallback_release"
            release_note = (
                f"fallback_reason={fallback.get('fallback_reason', '')}; "
                f"fallback_source_release_id={fallback.get('fallback_source_release_id', '')}; "
                f"target_trade_date={trade_date}"
            )
            if str(fallback.get("summary_path", "") or "").strip():
                extra.extend(["--source-summary-path", str(fallback.get("summary_path", "")).strip()])
            if str(fallback.get("target_positions_path", "") or "").strip():
                extra.extend(["--source-target-positions-path", str(fallback.get("target_positions_path", "")).strip()])
        return (
            command
            + [
                "--mode",
                "release_only",
                "--release-source-mode",
                release_source_mode,
                "--release-note",
                release_note,
                "--release-trade-date",
                str(trade_date or ""),
            ]
            + extra
        )
    if phase_name == "research_refresh":
        return command + ["--mode", "research_only"]
    if phase_name == "release_refresh":
        return command + [
            "--mode",
            "release_only",
            "--release-source-mode",
            "normal",
            "--release-note",
            "preopen_refresh",
            "--release-trade-date",
            str(trade_date or ""),
        ]
    if phase_name == "preopen_gate":
        release_id = str(cycle_state.get("release_id", "") or "")
        extra = ["--mode", "execution_only", "--gate-only", "--ignore-window"]
        if release_id:
            extra.extend(["--release-id", release_id])
        extra.extend(
            [
                "--execution-mode",
                str(scheduler.get("simulation_execution_mode", "precision") or "precision"),
                "--precision-trade",
                "on" if _scheduler_bool(scheduler, "simulation_precision_trade", "simulation_precision_trade_enabled", True) else "off",
                "--ignore-market-panic-reduce-only",
                "on" if bool(scheduler.get("simulation_ignore_market_panic_reduce_only", True)) else "off",
                "--execution-namespace",
                str(scheduler.get("simulation_namespace", "simulation") or "simulation"),
            ]
        )
        return command + extra
    if phase_name == "simulation":
        release_id = str(cycle_state.get("release_id", "") or "")
        extra = [
            "--mode",
            "execution_only",
            "--execution-mode",
            str(scheduler.get("simulation_execution_mode", "precision") or "precision"),
            "--precision-trade",
            "on" if _scheduler_bool(scheduler, "simulation_precision_trade", "simulation_precision_trade_enabled", True) else "off",
            "--ignore-market-panic-reduce-only",
            "on" if bool(scheduler.get("simulation_ignore_market_panic_reduce_only", True)) else "off",
            "--allow-unfinished-orders-reconcile",
            "on" if bool(scheduler.get("simulation_allow_unfinished_orders_reconcile", False)) else "off",
            "--execution-namespace",
            str(scheduler.get("simulation_namespace", "simulation") or "simulation"),
        ]
        if release_id:
            extra.extend(["--release-id", release_id])
        return command + extra
    if phase_name == "shadow":
        release_id = str(cycle_state.get("release_id", "") or "")
        extra = [
            "--mode",
            "execution_only",
            "--execution-mode",
            str(scheduler.get("shadow_execution_mode", "precision") or "precision"),
            "--precision-trade",
            "on" if _scheduler_bool(scheduler, "shadow_precision_trade", "shadow_precision_trade_enabled", True) else "off",
            "--ignore-market-panic-reduce-only",
            "on" if bool(scheduler.get("shadow_ignore_market_panic_reduce_only", True)) else "off",
            "--allow-unfinished-orders-reconcile",
            "on" if bool(scheduler.get("shadow_allow_unfinished_orders_reconcile", False)) else "off",
            "--execution-namespace",
            str(scheduler.get("shadow_namespace", "shadow") or "shadow"),
            "--shadow-run",
        ]
        if release_id:
            extra.extend(["--release-id", release_id])
        return command + extra
    if phase_name == "midday_review":
        release_id = str(cycle_state.get("release_id", "") or "")
        extra = ["--mode", "midday_review_only"]
        if release_id:
            extra.extend(["--release-id", release_id])
        return command + extra
    if phase_name in {"afternoon_execution", "afternoon_shadow"}:
        plan = _phase_execution_plan(config, cycle_state, phase_name)
        release_id = str(plan.get("release_id", "") or cycle_state.get("release_id", "") or "")
        namespace = str(plan.get("namespace", "") or scheduler.get("simulation_namespace", "simulation") or "simulation")
        execution_mode = str(plan.get("execution_mode", scheduler.get("simulation_execution_mode", "precision")) or "precision")
        precision_trade = bool(plan.get("precision_trade_enabled", _scheduler_bool(scheduler, "simulation_precision_trade", "simulation_precision_trade_enabled", True)))
        ignore_panic_reduce_only = bool(plan.get("ignore_market_panic_reduce_only", scheduler.get("simulation_ignore_market_panic_reduce_only", True)))
        allow_unfinished_reconcile = bool(plan.get("allow_unfinished_orders_reconcile", False))
        extra = [
            "--mode",
            "execution_only",
            "--execution-mode",
            execution_mode,
            "--precision-trade",
            "on" if precision_trade else "off",
            "--ignore-market-panic-reduce-only",
            "on" if ignore_panic_reduce_only else "off",
            "--allow-unfinished-orders-reconcile",
            "on" if allow_unfinished_reconcile else "off",
            "--execution-namespace",
            namespace,
        ]
        if phase_name == "afternoon_shadow":
            extra.append("--shadow-run")
        if release_id:
            extra.extend(["--release-id", release_id])
        return command + extra
    if str(phase_name or "").startswith("intraday_tactical_"):
        release_id = str(cycle_state.get("release_id", "") or "")
        extra = [
            "--mode",
            "intraday_tactics_only",
            "--tactical-phase",
            str(phase_name),
            "--execution-mode",
            str(scheduler.get("simulation_execution_mode", "simulation") or "simulation"),
            "--precision-trade",
            "on" if _scheduler_bool(scheduler, "simulation_precision_trade", "simulation_precision_trade_enabled", True) else "off",
            "--ignore-market-panic-reduce-only",
            "on" if bool(scheduler.get("simulation_ignore_market_panic_reduce_only", True)) else "off",
            "--allow-unfinished-orders-reconcile",
            "on" if bool(scheduler.get("simulation_allow_unfinished_orders_reconcile", False)) else "off",
            "--execution-namespace",
            str(scheduler.get("simulation_namespace", "precision") or "precision"),
            "--ignore-window",
        ]
        if release_id:
            extra.extend(["--release-id", release_id])
        return command + extra
    raise ValueError(f"Unsupported phase command: {phase_name}")


def _phase_outcome_from_execution_payload(phase_name: str, payload: Dict[str, Any]) -> str:
    status = str(payload.get("status", "") or "")
    gate = dict(payload.get("gate", {}) or {})
    safety = dict(payload.get("safety", {}) or {})
    if phase_name == "preopen_gate":
        if not bool(gate.get("ok", False)):
            return "failed"
        if not bool(gate.get("calendar_ok", False)):
            return "skipped"
        if not bool(safety.get("allow_execution", False)):
            return "skipped"
        if not bool(gate.get("should_execute", False)):
            return "skipped"
        return "success"
    if status == "executed":
        return "success"
    if status in {"skipped", "safety_blocked"}:
        return "skipped"
    if status == "execution_error":
        return "failed"
    return "failed"


def _normalise_phase_result(
    config: Dict[str, Any],
    phase_name: str,
    trade_date: str,
    raw_result: Dict[str, Any],
) -> Dict[str, Any]:
    result_payload = dict(raw_result.get("result_payload", {}) or {})
    if bool(raw_result.get("timed_out", False)):
        phase_status = "timeout"
    elif phase_name in {"research", "release", "research_refresh", "release_refresh", "midday_review"}:
        phase_status = "success" if bool(raw_result.get("ok", False)) else "failed"
    elif phase_name in {"preopen_gate", "simulation", "shadow", "afternoon_execution", "afternoon_shadow"}:
        phase_status = _phase_outcome_from_execution_payload(phase_name, result_payload)
        if not raw_result.get("ok", False) and phase_status == "success":
            phase_status = "failed"
    else:
        phase_status = "failed"
    release_id = ""
    if phase_name in {"release", "release_refresh"}:
        release_id = str(result_payload.get("release_id", "") or "")
    elif phase_name in {"research", "research_refresh"}:
        latest_release = _latest_release_safe(config)
        if str(latest_release.get("trade_date", "") or "") == str(trade_date):
            release_id = str(latest_release.get("release_id", "") or "")
    elif phase_name == "midday_review":
        release_id = str(result_payload.get("release", {}).get("release_id", "") or "")
    else:
        release_id = str(result_payload.get("release", {}).get("release_id", "") or result_payload.get("gate", {}).get("release", {}).get("release_id", "") or "")
    error_message = str(raw_result.get("error_message", "") or "").strip()
    if not error_message and phase_status == "skipped":
        error_message = str(
            result_payload.get("reason", "")
            or result_payload.get("gate", {}).get("reason", "")
            or result_payload.get("safety", {}).get("halt_reason", "")
            or "phase_skipped"
        ).strip()
    return {
        "status": phase_status,
        "return_code": raw_result.get("return_code"),
        "release_id": release_id,
        "warning_count": int(raw_result.get("warning_count", 0) or 0),
        "error_message": error_message,
        "stdout_log": str(raw_result.get("stdout_log", "") or ""),
        "stderr_log": str(raw_result.get("stderr_log", "") or ""),
        "stdout_tail": list(raw_result.get("stdout_tail", []) or []),
        "stderr_tail": list(raw_result.get("stderr_tail", []) or []),
        "result_status": str(result_payload.get("status", result_payload.get("status_code", "")) or ""),
        "result_payload": result_payload,
    }


def _phase_state_final(entry: Dict[str, Any]) -> bool:
    return str(entry.get("status", "") or "") in FINAL_PHASE_STATUSES


def _phase_should_retry(
    config: Dict[str, Any],
    *,
    state: Dict[str, Any],
    phase_name: str,
    now: datetime,
    entry: Dict[str, Any],
) -> bool:
    status = str(entry.get("status", "") or "").strip()
    error_message = str(entry.get("error_message", "") or "").strip()
    specs = _phase_specs(config)
    if phase_name == "research_refresh":
        cutoff = _scheduled_wallclock(now, specs["preopen_gate"].scheduled_time)
        return status == "failed" and error_message == "data_consistency_gate_failed" and now < cutoff
    if phase_name == "release_refresh":
        cutoff = _scheduled_wallclock(now, specs["preopen_gate"].scheduled_time)
        refresh_status = str(dict(state.get("phases", {}).get("research_refresh", {}) or {}).get("status", "") or "").strip()
        return (
            status == "skipped"
            and error_message == "research_refresh_not_ready"
            and refresh_status in {"success", "skipped", "timeout"}
            and now < cutoff
        )
    return False


def _phase_schedule_expired(
    config: Dict[str, Any],
    *,
    phase_name: str,
    now: datetime,
    scheduled_at: datetime,
) -> bool:
    # Future-date planning phases and end-of-day summary remain replayable; intraday phases do not.
    if phase_name in {"research", "release", "summary"}:
        return False
    deadline = _phase_forced_deadline(config, phase_name, now, scheduled_at)
    return deadline is not None and now >= deadline


def _sync_runtime_state(
    config: Dict[str, Any],
    profile: str,
    payload: Dict[str, Any],
) -> None:
    current = _load_json(_scheduler_runtime_state_path(config), default={})
    current.update(payload)
    current.setdefault("service_profile", str(profile or ""))
    current["updated_at"] = clock_now().isoformat(timespec="seconds")
    _write_json(_scheduler_runtime_state_path(config), current)

def _latest_release_for_trade_date(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    latest_release = _latest_release_safe(config)
    if str(latest_release.get("trade_date", "") or "") == str(trade_date):
        return latest_release
    return {}


def _daily_release_summary(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    if not release_doc:
        return {"available": False}
    fallback_active = str(release_doc.get("source_mode", "") or "") == "fallback_release" or "fallback_reason=" in str(release_doc.get("note", "") or "")
    return {
        "available": True,
        "release_id": str(release_doc.get("release_id", "") or ""),
        "trade_date": str(release_doc.get("trade_date", "") or ""),
        "profile": str(release_doc.get("profile", "") or ""),
        "source_mode": str(release_doc.get("source_mode", "") or ""),
        "generated_at": str(release_doc.get("generated_at", "") or ""),
        "target_count": int(release_doc.get("target_count", 0) or 0),
        "simulation_ready": bool(release_doc.get("simulation_ready", False)),
        "fallback_active": fallback_active,
        "note": str(release_doc.get("note", "") or ""),
    }


def _portfolio_summary(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    if not release_doc:
        return {"available": False}
    posture = dict(release_doc.get("portfolio_posture", {}) or {})
    v2a = dict(release_doc.get("portfolio", {}) or {})
    return {
        "available": True,
        "state_counts": dict(v2a.get("state_counts", {}) or {}),
        "action_counts": dict(v2a.get("action_counts", {}) or {}),
        "new_entry_count": int(v2a.get("new_entry_count", 0) or 0),
        "replacement_count": int(v2a.get("replacement_count", 0) or 0),
        "total_exposure_cap": float(posture.get("total_exposure_cap", 0.0) or 0.0),
        "new_entry_budget": float(posture.get("new_entry_budget", 0.0) or 0.0),
        "rebalance_mode": str(posture.get("rebalance_mode", "") or ""),
        "current_position_count": int(posture.get("current_position_count", 0) or 0),
        "weak_existing_count": int(posture.get("weak_existing_count", 0) or 0),
    }


def _latest_portfolio_operator_guidance(config: Dict[str, Any]) -> Dict[str, Any]:
    portfolio_root = Path(str(config.get("paths", {}).get("portfolio_output_root", "") or "")).resolve()
    summary_path = portfolio_root / "portfolio_recommendation.json"
    payload = _load_json(summary_path, default={})
    if not payload:
        return {"available": False, "summary_path": str(summary_path)}
    lifecycle = dict(payload.get("alpha_lifecycle", {}) or {})
    operating_brain = dict(payload.get("llm_operating_brain", {}) or {})
    review = dict(operating_brain.get("review", {}) or {})
    dispatch = dict(review.get("dispatch_brain", {}) or {})
    operations = dict(review.get("operations_brain", {}) or {})
    return {
        "available": True,
        "summary_path": str(summary_path),
        "generated_at": str(payload.get("generated_at", "") or ""),
        "promote_families": list(lifecycle.get("promote_families", []) or []),
        "demote_families": list(lifecycle.get("demote_families", []) or []),
        "shadow_families": list(lifecycle.get("shadow_families", []) or []),
        "preferred_posture": str(dispatch.get("preferred_posture", "") or ""),
        "cash_posture": str(dispatch.get("cash_posture", "") or ""),
        "tactical_bias": str(dispatch.get("tactical_bias", "") or ""),
        "watch_items": list(operations.get("watch_items", []) or []),
        "incident_actions": list(operations.get("incident_actions", []) or []),
        "uncertainty_flags": list(review.get("uncertainty_flags", []) or []),
        "overfit_guard": str(review.get("overfit_guard", "") or ""),
    }


def _market_state_summary(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    market_state = dict(release_doc.get("market_state", {}) or {})
    if not market_state:
        return {"available": False}
    market_state["available"] = True
    return market_state


def _integrated_thesis_summary(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(release_doc.get("integrated_thesis_state", {}) or {})
    if not payload:
        return {"available": False}
    return {
        "available": True,
        "formal_strategy_framework": str(payload.get("formal_strategy_framework", "integrated_event_industry_earnings_alpha") or "integrated_event_industry_earnings_alpha"),
        "primary_strategy_key": str(payload.get("primary_strategy_key", "") or ""),
        "portfolio_construction": dict(payload.get("portfolio_construction", {}) or {}),
        "summary": dict(payload.get("summary", {}) or {}),
        "top_candidates": list(payload.get("top_candidates", []) or [])[:10],
    }


def _oms_root_for_namespace(config: Dict[str, Any], namespace: str) -> Path:
    base_root = Path(str(config.get("oms", {}).get("output_root", config.get("paths", {}).get("oms_output_root", "")) or "")).resolve()
    if str(namespace or "").strip() and str(namespace or "").strip() != "main":
        return base_root / str(namespace).strip()
    return base_root


def _oms_summary_for_namespace(config: Dict[str, Any], namespace: str) -> Dict[str, Any]:
    oms_root = _oms_root_for_namespace(config, namespace)
    summary_path = oms_root / "snapshots" / "oms_summary.json"
    summary = _load_json(summary_path, default={})
    if not summary:
        return {"available": False, "namespace": namespace, "oms_summary_path": str(summary_path)}
    summary["available"] = True
    summary["namespace"] = namespace
    summary["oms_summary_path"] = str(summary_path)
    return summary


def _phase_status_for_pack(config: Dict[str, Any], trade_date: str, cycle_state: Dict[str, Any]) -> Dict[str, Any]:
    latest_state = _load_json(_phase_state_path(config, trade_date), default=cycle_state)
    pack_state = copy.deepcopy(latest_state if latest_state else cycle_state)
    phases = dict(pack_state.get("phases", {}) or {})
    now_iso = clock_now().isoformat(timespec="seconds")
    scheduler = _scheduler_cfg(config)

    for phase_name in _phase_sequence(config):
        entry = _empty_phase_state()
        entry.update(dict(phases.get(phase_name, {}) or {}))
        if phase_name == "summary" and str(entry.get("status", "") or "") == "running":
            entry["status"] = "success"
            entry["finished_at"] = now_iso
            entry["result_status"] = str(entry.get("result_status", "") or "summary_success")
        if phase_name == "shadow" and not bool(scheduler.get("shadow_enabled", False)) and not _phase_state_final(entry):
            entry.update(
                {
                    "status": "skipped",
                    "finished_at": str(entry.get("finished_at", "") or now_iso),
                    "error_message": "automatic_shadow_disabled",
                    "result_status": "automatic_shadow_disabled",
                    "result_payload": {"reason": "automatic_shadow_disabled"},
                }
            )
        if phase_name == "afternoon_shadow" and not bool(scheduler.get("afternoon_shadow_enabled", False)) and not _phase_state_final(entry):
            entry.update(
                {
                    "status": "skipped",
                    "finished_at": str(entry.get("finished_at", "") or now_iso),
                    "error_message": "automatic_shadow_disabled",
                    "result_status": "automatic_shadow_disabled",
                    "result_payload": {"reason": "automatic_shadow_disabled"},
                }
            )
        phases[phase_name] = entry

    external_release = dict(pack_state.get("external_release_adopted", {}) or {})
    adopted_release_id = str(external_release.get("release_id", "") or "").strip()
    if bool(external_release.get("active", False)) and adopted_release_id:
        adopted_at = str(external_release.get("adopted_at", "") or now_iso)
        release_entry = _empty_phase_state()
        release_entry.update(dict(phases.get("release", {}) or {}))
        if not _phase_state_final(release_entry):
            release_entry.update(
                {
                    "status": "success",
                    "started_at": str(release_entry.get("started_at", "") or adopted_at),
                    "finished_at": str(release_entry.get("finished_at", "") or adopted_at),
                    "release_id": adopted_release_id,
                    "error_message": "external_release_adopted",
                    "result_status": "external_release_adopted",
                    "result_payload": {
                        "reason": "external_release_adopted",
                        "external_release_adopted": external_release,
                    },
                }
            )
            phases["release"] = release_entry
        research_entry = _empty_phase_state()
        research_entry.update(dict(phases.get("research", {}) or {}))
        if not _phase_state_final(research_entry):
            research_entry.update(
                {
                    "status": "skipped",
                    "finished_at": str(research_entry.get("finished_at", "") or adopted_at),
                    "error_message": "external_release_adopted_no_scheduler_trace",
                    "result_status": "external_release_adopted_no_scheduler_trace",
                    "result_payload": {
                        "reason": "external_release_adopted_no_scheduler_trace",
                        "external_release_adopted": external_release,
                    },
                }
            )
            phases["research"] = research_entry

    pack_state["phases"] = phases
    pack_state["current_phase"] = ""
    pack_state["updated_at"] = now_iso
    return pack_state


def _validated_oms_summary(
    config: Dict[str, Any],
    namespace: str,
    expected_release_id: str,
    phase_entry: Dict[str, Any],
) -> Dict[str, Any]:
    phase_status = str(dict(phase_entry or {}).get("status", "") or "")
    summary = _oms_summary_for_namespace(config, namespace)
    return _validate_oms_summary_payload(
        summary=summary,
        namespace=namespace,
        expected_release_id=expected_release_id,
        phase_status=phase_status,
    )


def _validate_oms_summary_payload(
    summary: Dict[str, Any],
    namespace: str,
    expected_release_id: str,
    phase_status: str,
) -> Dict[str, Any]:
    summary = dict(summary or {})
    summary["phase_status"] = phase_status
    if not bool(summary.get("available", False)):
        summary["unavailable_reason"] = "missing_summary"
        return summary
    stale_reasons: list[str] = []
    actual_release_id = str(summary.get("release_id", "") or "").strip()
    if phase_status != "success":
        stale_reasons.append(f"phase_status_{phase_status or 'unknown'}")
    if expected_release_id and actual_release_id != expected_release_id:
        stale_reasons.append("release_id_mismatch")
    if stale_reasons:
        return {
            "available": False,
            "namespace": namespace,
            "oms_summary_path": str(summary.get("oms_summary_path", "") or ""),
            "generated_at": str(summary.get("generated_at", "") or ""),
            "release_id": actual_release_id,
            "phase_status": phase_status,
            "stale_reasons": stale_reasons,
            "unavailable_reason": "stale_snapshot",
        }
    return summary


def _phase_embedded_oms_summary(phase_entry: Dict[str, Any], namespace: str) -> Dict[str, Any]:
    payload = dict(dict(phase_entry or {}).get("result_payload", {}) or {})
    execution_report = dict(payload.get("execution_report", {}) or {})
    oms_bucket = dict(execution_report.get("oms", {}) or {})
    embedded = dict(oms_bucket.get("summary", {}) or {})
    if not embedded:
        report_path = Path(str(payload.get("execution_report_path", "") or "").strip())
        if report_path.exists():
            report_doc = _load_json(report_path, default={})
            oms_bucket = dict(report_doc.get("oms", {}) or {})
            embedded = dict(oms_bucket.get("summary", {}) or {})
    if not embedded:
        return {}
    embedded["available"] = True
    embedded["namespace"] = namespace
    embedded["oms_summary_path"] = str(oms_bucket.get("summary_path", "") or embedded.get("oms_summary_path", "") or "")
    return embedded


def _phase_or_latest_oms_summary(
    config: Dict[str, Any],
    namespace: str,
    expected_release_id: str,
    phase_entry: Dict[str, Any],
) -> Dict[str, Any]:
    phase_status = str(dict(phase_entry or {}).get("status", "") or "")
    embedded = _phase_embedded_oms_summary(phase_entry, namespace)
    if embedded:
        validated = _validate_oms_summary_payload(
            summary=embedded,
            namespace=namespace,
            expected_release_id=expected_release_id,
            phase_status=phase_status,
        )
        if bool(validated.get("available", False)):
            validated["source"] = "phase_execution_report"
        return validated
    validated = _validated_oms_summary(
        config=config,
        namespace=namespace,
        expected_release_id=expected_release_id,
        phase_entry=phase_entry,
    )
    if bool(validated.get("available", False)):
        validated["source"] = "latest_snapshot"
    return validated


def _gap_diagnostics(oms_summary: Dict[str, Any]) -> Dict[str, Any]:
    if not bool(oms_summary.get("available", False)):
        return {
            "available": False,
            "primary_cause": "unavailable",
            "drivers": [],
        }
    dispatch = dict(oms_summary.get("dispatch", {}) or {})
    continuity = dict(oms_summary.get("continuity", {}) or {})
    gap = dict(oms_summary.get("gap", {}) or {})
    drivers: list[str] = []
    turnover_truncation_ratio = float(dispatch.get("turnover_truncation_ratio", 0.0) or 0.0)
    n_dispatch_orders = int(dispatch.get("n_dispatch_orders", 0) or 0)
    n_fills = int(dispatch.get("n_fills", 0) or 0)
    n_open_intents_after = int(continuity.get("n_open_intents_after", 0) or 0)
    n_carried_symbols = int(continuity.get("n_carried_symbols", 0) or 0)
    gap_weight_ratio = float(gap.get("gap_weight_ratio", 0.0) or 0.0)
    if turnover_truncation_ratio >= 0.25:
        drivers.append("turnover_budget_truncation")
    if n_open_intents_after > 0 or n_carried_symbols > 0:
        drivers.append("oms_continuity_carryover")
    if n_dispatch_orders > 0 and n_fills < n_dispatch_orders:
        drivers.append("execution_fill_friction")
    if gap_weight_ratio > 0 and not drivers:
        drivers.append("residual_portfolio_gap")
    return {
        "available": True,
        "primary_cause": drivers[0] if drivers else "none",
        "drivers": drivers,
        "turnover_truncation_ratio": turnover_truncation_ratio,
        "n_dispatch_orders": n_dispatch_orders,
        "n_fills": n_fills,
        "n_open_intents_after": n_open_intents_after,
        "n_carried_symbols": n_carried_symbols,
        "gap_weight_ratio": gap_weight_ratio,
    }


def _build_daily_pack(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
    cycle_state: Dict[str, Any],
) -> Dict[str, Any]:
    pack_dir = ensure_dir(_automation_runs_root(config) / trade_date.replace("-", ""))
    logs_dir = ensure_dir(pack_dir / "logs")
    pack_state = _phase_status_for_pack(config=config, trade_date=trade_date, cycle_state=cycle_state)
    release_doc = _release_by_id_safe(config, str(pack_state.get("release_id", "") or "")) or _latest_release_for_trade_date(config, trade_date)
    release_summary = _daily_release_summary(release_doc)
    market_summary = _market_state_summary(release_doc)
    integrated_thesis_summary = _integrated_thesis_summary(release_doc)
    v2a_summary = _portfolio_summary(release_doc)
    scheduler = _scheduler_cfg(config)
    midday_plan = _midday_plan_payload(pack_state)
    simulation_namespace = str(midday_plan.get("real_execution", {}).get("namespace", "") or scheduler.get("simulation_namespace", "simulation") or "simulation")
    shadow_namespace = str(midday_plan.get("shadow_execution", {}).get("namespace", "") or scheduler.get("shadow_namespace", "shadow") or "shadow")
    simulation_phase = dict(pack_state.get("phases", {}).get("simulation", {}) or {})
    shadow_phase = dict(pack_state.get("phases", {}).get("shadow", {}) or {})
    simulation_oms = _phase_or_latest_oms_summary(
        config=config,
        namespace=simulation_namespace,
        expected_release_id=str(release_summary.get("release_id", "") or ""),
        phase_entry=simulation_phase,
    )
    shadow_oms = _phase_or_latest_oms_summary(
        config=config,
        namespace=shadow_namespace,
        expected_release_id=str(release_summary.get("release_id", "") or ""),
        phase_entry=shadow_phase,
    )
    simulation_gap_analysis = _gap_diagnostics(simulation_oms)
    shadow_gap_analysis = _gap_diagnostics(shadow_oms)
    safety_state = _load_json(_trade_clock_root(config) / "system_safety_state.json", default={})
    intraday_manifest = _load_json(_latest_intraday_manifest_path(config), default={})
    intraday_phase_state = _load_json(Path(str(intraday_manifest.get("phase_state_path", "") or "")).resolve(), default={}) if str(intraday_manifest.get("phase_state_path", "") or "").strip() else {}
    intraday_control_summary = _load_json(Path(str(intraday_manifest.get("control_summary_path", "") or "")).resolve(), default={}) if str(intraday_manifest.get("control_summary_path", "") or "").strip() else {}
    operator_guidance = _latest_portfolio_operator_guidance(config)
    phase_status_path = _phase_state_path(config, trade_date)
    warnings: list[Dict[str, Any]] = []
    critical_flags: list[Dict[str, Any]] = []
    for phase_name in _phase_sequence(config):
        phase_entry = dict(pack_state.get("phases", {}).get(phase_name, {}) or {})
        status = str(phase_entry.get("status", "") or "")
        if status in {"failed", "timeout"}:
            critical_flags.append({"phase": phase_name, "code": status, "message": str(phase_entry.get("error_message", "") or status)})
        elif status == "skipped":
            warnings.append({"phase": phase_name, "code": "skipped", "message": str(phase_entry.get("error_message", "") or "phase_skipped")})
        for stream_name in ("stdout_log", "stderr_log"):
            raw = str(phase_entry.get(stream_name, "") or "").strip()
            if not raw:
                continue
            src = Path(raw)
            if src.exists():
                suffix = "stdout" if stream_name == "stdout_log" else "stderr"
                shutil.copy2(src, logs_dir / f"{phase_name}.{suffix}.log")
    if not release_summary.get("available", False):
        critical_flags.append({"phase": "release", "code": "missing_release", "message": "No formal release was available for this trade date."})
    if str(safety_state.get("system_mode", "") or "") not in {"", "NORMAL"}:
        warnings.append(
            {
                "phase": "safety",
                "code": str(safety_state.get("system_mode", "") or "UNKNOWN"),
                "message": str(safety_state.get("halt_reason", "") or safety_state.get("market_safety_regime", "") or "safety_not_normal"),
            }
        )
    if int(simulation_oms.get("gap", {}).get("n_gap_symbols", 0) or 0) > 0:
        warnings.append(
            {
                "phase": "simulation",
                "code": "oms_gap",
                "message": (
                    f"simulation gap symbols={simulation_oms.get('gap', {}).get('n_gap_symbols', 0)} "
                    f"primary_cause={simulation_gap_analysis.get('primary_cause', 'unknown')}"
                ),
            }
        )
    elif str(simulation_oms.get("unavailable_reason", "") or "") == "stale_snapshot":
        warnings.append(
            {
                "phase": "simulation",
                "code": "stale_oms_summary",
                "message": f"simulation summary stale reasons={','.join(list(simulation_oms.get('stale_reasons', []) or []))}",
            }
        )
    if int(shadow_oms.get("gap", {}).get("n_gap_symbols", 0) or 0) > 0:
        warnings.append(
            {
                "phase": "shadow",
                "code": "oms_gap",
                "message": (
                    f"shadow gap symbols={shadow_oms.get('gap', {}).get('n_gap_symbols', 0)} "
                    f"primary_cause={shadow_gap_analysis.get('primary_cause', 'unknown')}"
                ),
            }
        )
    elif str(shadow_oms.get("unavailable_reason", "") or "") == "stale_snapshot" and str(shadow_phase.get("status", "") or "") == "success":
        warnings.append(
            {
                "phase": "shadow",
                "code": "stale_oms_summary",
                "message": f"shadow summary stale reasons={','.join(list(shadow_oms.get('stale_reasons', []) or []))}",
            }
        )

    _write_json(pack_dir / "phase_status.json", pack_state)
    _write_json(pack_dir / "daily_release_summary.json", release_summary)
    _write_json(pack_dir / "market_state_summary.json", market_summary)
    _write_json(pack_dir / "integrated_thesis_summary.json", integrated_thesis_summary)
    _write_json(pack_dir / "portfolio_summary.json", v2a_summary)
    _write_json(pack_dir / "oms_summary_simulation.json", simulation_oms)
    _write_json(pack_dir / "oms_summary_shadow.json", shadow_oms)
    _write_json(pack_dir / "gap_analysis_simulation.json", simulation_gap_analysis)
    _write_json(pack_dir / "gap_analysis_shadow.json", shadow_gap_analysis)
    _write_json(pack_dir / "intraday_phase_state.json", intraday_phase_state)
    _write_json(pack_dir / "intraday_control_summary.json", intraday_control_summary)
    _write_json(pack_dir / "operator_guidance.json", operator_guidance)
    _write_json(pack_dir / "warnings.json", {"count": len(warnings), "items": warnings})
    _write_json(pack_dir / "critical_flags.json", {"count": len(critical_flags), "items": critical_flags})
    for manifest_key, target_name in (
        ("symbol_state_path", "symbol_execution_state.csv"),
        ("intent_state_path", "intent_state_daily.csv"),
        ("event_log_path", "intraday_event_log.jsonl"),
    ):
        source = Path(str(intraday_manifest.get(manifest_key, "") or "")).resolve() if str(intraday_manifest.get(manifest_key, "") or "").strip() else Path()
        if source.exists():
            shutil.copy2(source, pack_dir / target_name)
    strategy_audit = build_strategy_audit_pack(
        config=config,
        trade_date=trade_date,
        release_doc=release_doc,
        pack_dir=pack_dir,
    )
    if phase_status_path.exists():
        shutil.copy2(phase_status_path, pack_dir / "phase_status.source.json")

    phase_overview = {
        phase_name: str(dict(pack_state.get("phases", {}).get(phase_name, {}) or {}).get("status", "") or "")
            for phase_name in _phase_sequence(config)
    }
    target_count = int(release_doc.get("target_count", 0) or release_summary.get("target_count", 0) or 0)
    report_lines = [
        f"日期: {trade_date}",
        f"研究档位: {profile}",
        f"release_id: {release_summary.get('release_id', '') or '无'}",
        f"market_regime: {market_summary.get('market_regime', '') or 'unknown'}",
        f"primary_strategy: {integrated_thesis_summary.get('primary_strategy_key', '') or 'unknown'}",
        f"style_bias: {market_summary.get('style_bias', '') or 'unknown'}",
        f"V2A posture: {v2a_summary.get('rebalance_mode', '') or 'unknown'}",
        f"dispatch_posture: {operator_guidance.get('preferred_posture', '') or 'unknown'}",
        f"tactical_bias: {operator_guidance.get('tactical_bias', '') or 'unknown'}",
        f"alpha_promote: {', '.join(list(operator_guidance.get('promote_families', []) or [])[:4]) or 'none'}",
        f"目标持仓数: {target_count}",
        f"simulation: {phase_overview.get('simulation', '') or 'queued'}",
        f"shadow: {phase_overview.get('shadow', '') or 'queued'}",
        f"midday_review: {phase_overview.get('midday_review', '') or 'queued'}",
        f"afternoon_execution: {phase_overview.get('afternoon_execution', '') or 'queued'}",
        f"afternoon_shadow: {phase_overview.get('afternoon_shadow', '') or 'queued'}",
        f"intraday_phase: {intraday_phase_state.get('current_phase', '') or 'unknown'}",
        f"intraday_midday_action: {intraday_control_summary.get('midday_action', '') or 'unknown'}",
        f"OMS gap(simulation): {simulation_oms.get('gap', {}).get('n_gap_symbols', 0) if simulation_oms.get('available', False) else 'n/a'}",
        f"OMS gap(shadow): {shadow_oms.get('gap', {}).get('n_gap_symbols', 0) if shadow_oms.get('available', False) else 'n/a'}",
        f"simulation gap primary_cause: {simulation_gap_analysis.get('primary_cause', 'unknown')}",
        f"overfit_risk: {dict(strategy_audit.get('payload', {}) or {}).get('overfit_risk', {}).get('risk_level', 'unknown')}",
        f"shadow data_state: {shadow_oms.get('unavailable_reason', 'fresh') if not shadow_oms.get('available', False) else 'fresh'}",
        "Top warnings:",
    ]
    report_lines[0:3] = [
        f"trade_date: {trade_date}",
        f"research_profile: {profile}",
        f"release_id: {release_summary.get('release_id', '') or 'unknown'}",
    ]
    if len(report_lines) > 10:
        report_lines[10] = f"target_count: {target_count}"
    if warnings:
        for item in warnings[:6]:
            report_lines.append(f"- [{item.get('phase', '')}] {item.get('message', '')}")
    else:
        report_lines.append("- none")
    _write_text(pack_dir / "daily_report.txt", "\n".join(report_lines) + "\n")
    _write_text(pack_dir / "daily_report.md", "# Daily Automation Report\n\n" + "\n".join(report_lines) + "\n")
    manifest = {
        "generated_at": clock_now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "scheduler_profile": str(profile or ""),
        "release_id": str(release_summary.get("release_id", "") or ""),
        "pack_dir": str(pack_dir),
        "phase_status_path": str(pack_dir / "phase_status.json"),
        "intraday_phase_state_path": str(pack_dir / "intraday_phase_state.json"),
        "intraday_control_summary_path": str(pack_dir / "intraday_control_summary.json"),
        "operator_guidance_path": str(pack_dir / "operator_guidance.json"),
        "operator_guidance": operator_guidance,
        "simulation_namespace": simulation_namespace,
        "shadow_namespace": shadow_namespace,
        "phase_overview": phase_overview,
        "warning_count": len(warnings),
        "critical_count": len(critical_flags),
        "report_path": str(pack_dir / "daily_report.txt"),
        "strategy_audit_json_path": str(strategy_audit.get("json_path", "") or ""),
        "strategy_audit_html_path": str(strategy_audit.get("html_path", "") or ""),
    }
    _write_json(pack_dir / "run_manifest.json", manifest)
    return manifest


def _run_summary_phase(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
    cycle_state: Dict[str, Any],
) -> Dict[str, Any]:
    logs = _runtime_log_paths(config, trade_date, "summary")
    lock = _acquire_lock(config, _phase_specs(config)["summary"].lock_name, trade_date=trade_date, phase_name="summary")
    if lock is None:
        return {
            "status": "skipped",
            "return_code": None,
            "release_id": str(cycle_state.get("release_id", "") or ""),
            "warning_count": 0,
            "error_message": "lock_held",
            "stdout_log": str(logs["stdout"]),
            "stderr_log": str(logs["stderr"]),
            "stdout_tail": [],
            "stderr_tail": [],
            "result_status": "",
            "result_payload": {},
        }
    try:
        current_cycle_state = _ensure_cycle_state(config, trade_date, profile)
        intraday_refresh = _run_intraday_state_refresh(config=config, trade_date=trade_date, source_phase="summary", cycle_state=current_cycle_state)
        manifest = _build_daily_pack(config=config, trade_date=trade_date, profile=profile, cycle_state=current_cycle_state)
        if intraday_refresh.get("ran", False):
            manifest["intraday_state_machine"] = intraday_refresh
        publish_result = _run_audit_site_publish(config=config, trade_date=trade_date, report_dir=Path(str(manifest.get("pack_dir", "") or "")).resolve())
        if publish_result.get("ran", False):
            manifest["audit_site_publish"] = publish_result
        if publish_result.get("ran", False) and not publish_result.get("ok", False) and not publish_result.get("fail_open", True):
            raise RuntimeError(publish_result.get("message", "") or "audit_site_publish_failed")
        text = (
            f"summary_success trade_date={trade_date} "
            f"release_id={manifest.get('release_id', '')} "
            f"pack_dir={manifest.get('pack_dir', '')}"
        )
        _write_text(logs["stdout"], text + "\n")
        _write_text(logs["stderr"], "")
        return {
            "status": "success",
            "return_code": 0,
            "release_id": str(manifest.get("release_id", "") or ""),
            "warning_count": 0,
            "error_message": "",
            "stdout_log": str(logs["stdout"]),
            "stderr_log": str(logs["stderr"]),
            "stdout_tail": [text],
            "stderr_tail": [],
            "result_status": "summary_success",
            "result_payload": manifest,
        }
    except Exception as exc:
        _write_text(logs["stdout"], "")
        _write_text(logs["stderr"], f"{exc}\n")
        return {
            "status": "failed",
            "return_code": 1,
            "release_id": str(cycle_state.get("release_id", "") or ""),
            "warning_count": 0,
            "error_message": str(exc),
            "stdout_log": str(logs["stdout"]),
            "stderr_log": str(logs["stderr"]),
            "stdout_tail": [],
            "stderr_tail": [str(exc)],
            "result_status": "summary_failed",
            "result_payload": {},
        }
    finally:
        _release_lock(config, _phase_specs(config)["summary"].lock_name)

def _mark_phase_running(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
    phase_name: str,
    scheduled_for: str,
) -> Dict[str, Any]:
    state = _ensure_cycle_state(config, trade_date, profile)
    phase_entry = dict(state.get("phases", {}).get(phase_name, {}) or {})
    phase_entry.update(
        {
            "status": "running",
            "scheduled_for": scheduled_for,
            "started_at": clock_now().isoformat(timespec="seconds"),
            "finished_at": "",
            "error_message": "",
        }
    )
    state["current_phase"] = phase_name
    state["phases"][phase_name] = phase_entry
    _save_cycle_state(config, state)
    return state


def _mark_phase_complete(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
    phase_name: str,
    phase_result: Dict[str, Any],
) -> Dict[str, Any]:
    state = _ensure_cycle_state(config, trade_date, profile)
    phase_entry = dict(state.get("phases", {}).get(phase_name, {}) or {})
    phase_entry.update(phase_result)
    phase_entry["finished_at"] = clock_now().isoformat(timespec="seconds")
    state["phases"][phase_name] = phase_entry
    if phase_name == "release" and str(phase_result.get("release_id", "") or "").strip():
        state["release_id"] = str(phase_result.get("release_id", "") or "").strip()
    if phase_name == "summary" and isinstance(phase_result.get("result_payload"), dict):
        state["summary_pack_dir"] = str(phase_result["result_payload"].get("pack_dir", "") or "")
    state["current_phase"] = ""
    _save_cycle_state(config, state)
    intraday_refresh = _run_intraday_state_refresh(config=config, trade_date=trade_date, source_phase=phase_name, cycle_state=state)
    if intraday_refresh.get("ran", False):
        phase_entry = dict(state.get("phases", {}).get(phase_name, {}) or {})
        phase_entry["intraday_state_machine"] = intraday_refresh
        state["phases"][phase_name] = phase_entry
        if isinstance(intraday_refresh.get("manifest"), dict):
            state["latest_intraday_state_manifest"] = dict(intraday_refresh.get("manifest", {}) or {})
        _save_cycle_state(config, state)
    return state


def _mark_phase_exception(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
    phase_name: str,
    exc: Exception,
) -> Dict[str, Any]:
    operator_guidance = _latest_portfolio_operator_guidance(config)
    return _mark_phase_complete(
        config,
        trade_date,
        profile,
        phase_name,
        {
            "status": "failed",
            "return_code": None,
            "release_id": "",
            "warning_count": 0,
            "error_message": f"phase_exception: {exc}",
            "stdout_log": "",
            "stderr_log": "",
            "stdout_tail": [],
            "stderr_tail": [str(exc)],
            "result_status": "phase_exception",
            "result_payload": {
                "operator_guidance": operator_guidance,
                "incident_actions": list(operator_guidance.get("incident_actions", []) or []),
                "watch_items": list(operator_guidance.get("watch_items", []) or []),
            },
        },
    )


def _adopt_external_release_for_trade_date(
    config: Dict[str, Any],
    trade_date: str,
    profile: str,
) -> Dict[str, Any]:
    state = _ensure_cycle_state(config, trade_date, profile)
    latest_release = _latest_release_for_trade_date(config, trade_date)
    latest_release_id = str(latest_release.get("release_id", "") or "").strip()
    current_release_id = str(state.get("release_id", "") or "").strip()
    if not latest_release_id or latest_release_id == current_release_id:
        return state
    state["release_id"] = latest_release_id
    state["external_release_adopted"] = {
        "active": True,
        "adopted_at": clock_now().isoformat(timespec="seconds"),
        "release_id": latest_release_id,
        "source_mode": str(latest_release.get("source_mode", "") or ""),
        "generated_at": str(latest_release.get("generated_at", "") or ""),
        "trade_date": str(latest_release.get("trade_date", "") or ""),
    }
    _save_cycle_state(config, state)
    return state


def _candidate_phases(config: Dict[str, Any], profile: str, now: datetime) -> list[Dict[str, Any]]:
    specs = _phase_specs(config)
    current_trade_date = _current_trade_date(config, now)
    next_trade_date = _next_trade_date(config, now.date())
    candidates: list[Dict[str, Any]] = []
    if next_trade_date:
        candidates.extend(
            [
                {"trade_date": next_trade_date, "phase_name": "research", "scheduled_at": _scheduled_wallclock(now, specs["research"].scheduled_time)},
                {"trade_date": next_trade_date, "phase_name": "release", "scheduled_at": _scheduled_wallclock(now, specs["release"].scheduled_time)},
            ]
        )
    if current_trade_date:
        current_candidates = [
            {"trade_date": current_trade_date, "phase_name": "research_refresh", "scheduled_at": _scheduled_wallclock(now, specs["research_refresh"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "release_refresh", "scheduled_at": _scheduled_wallclock(now, specs["release_refresh"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "preopen_gate", "scheduled_at": _scheduled_wallclock(now, specs["preopen_gate"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "simulation", "scheduled_at": _scheduled_wallclock(now, specs["simulation"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "midday_review", "scheduled_at": _scheduled_wallclock(now, specs["midday_review"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "afternoon_execution", "scheduled_at": _scheduled_wallclock(now, specs["afternoon_execution"].scheduled_time)},
            {"trade_date": current_trade_date, "phase_name": "summary", "scheduled_at": _scheduled_wallclock(now, specs["summary"].scheduled_time)},
        ]
        scheduler = _scheduler_cfg(config)
        if not bool(scheduler.get("morning_research_refresh_enabled", True)):
            current_candidates = [item for item in current_candidates if item["phase_name"] != "research_refresh"]
        if not bool(scheduler.get("morning_release_refresh_enabled", True)):
            current_candidates = [item for item in current_candidates if item["phase_name"] != "release_refresh"]
        if bool(scheduler.get("shadow_enabled", False)):
            current_candidates.append({"trade_date": current_trade_date, "phase_name": "shadow", "scheduled_at": _scheduled_wallclock(now, specs["shadow"].scheduled_time)})
        if bool(scheduler.get("afternoon_shadow_enabled", False)):
            current_candidates.append({"trade_date": current_trade_date, "phase_name": "afternoon_shadow", "scheduled_at": _scheduled_wallclock(now, specs["afternoon_shadow"].scheduled_time)})
        if bool(dict(config.get("intraday_tactics", {}) or {}).get("enabled", True)):
            tac_sched = dict(dict(config.get("intraday_tactics", {}) or {}).get("scheduler_phases", {}) or {})
            for name in tactical_phase_names(config, enabled_only=True):
                row = dict(tac_sched.get(name, {}) or {})
                if not bool(row.get("enabled", True)):
                    continue
                current_candidates.append(
                    {"trade_date": current_trade_date, "phase_name": name, "scheduled_at": _scheduled_wallclock(now, specs[name].scheduled_time)}
                )
        candidates.extend(current_candidates)
    due: list[Dict[str, Any]] = []
    for item in candidates:
        if now < item["scheduled_at"]:
            continue
        if _phase_schedule_expired(
            config,
            phase_name=str(item["phase_name"]),
            now=now,
            scheduled_at=item["scheduled_at"],
        ):
            continue
        state = _ensure_cycle_state(config, item["trade_date"], profile)
        phase_entry = dict(state.get("phases", {}).get(item["phase_name"], {}) or {})
        if _phase_state_final(phase_entry) and not _phase_should_retry(
            config,
            state=state,
            phase_name=item["phase_name"],
            now=now,
            entry=phase_entry,
        ):
            continue
        due.append(item)
    sequence = _phase_sequence(config)
    return sorted(due, key=lambda row: (row["scheduled_at"], sequence.index(row["phase_name"]) if row["phase_name"] in sequence else 999))


def _run_phase(
    config: Dict[str, Any],
    profile: str,
    trade_date: str,
    phase_name: str,
    scheduled_at: datetime,
) -> Dict[str, Any]:
    cycle_state = _adopt_external_release_for_trade_date(config, trade_date, profile)
    specs = _phase_specs(config)
    _mark_phase_running(config, trade_date, profile, phase_name, scheduled_for=scheduled_at.isoformat(timespec="seconds"))
    pre_research_refresh: Dict[str, Any] = {}
    if phase_name == "release":
        research_status = str(dict(cycle_state.get("phases", {}).get("research", {}) or {}).get("status", "") or "")
        if research_status in {"failed", "timeout"}:
            fallback = _find_fallback_source(config, trade_date=trade_date)
            cycle_state["fallback"] = {
                "active": bool(fallback.get("ok", False)),
                **fallback,
            }
            _save_cycle_state(config, cycle_state)
            if not bool(fallback.get("ok", False)):
                result = {
                    "status": "failed",
                    "return_code": None,
                    "release_id": "",
                    "warning_count": 0,
                    "error_message": str(fallback.get("fallback_reason", "") or "fallback_unavailable"),
                    "stdout_log": "",
                    "stderr_log": "",
                    "stdout_tail": [],
                    "stderr_tail": [],
                    "result_status": "fallback_unavailable",
                    "result_payload": fallback,
                }
                return _mark_phase_complete(config, trade_date, profile, phase_name, result)
        else:
            cycle_state["fallback"] = {"active": False}
            _save_cycle_state(config, cycle_state)
    elif phase_name == "release_refresh":
        refresh_status = str(dict(cycle_state.get("phases", {}).get("research_refresh", {}) or {}).get("status", "") or "")
        if refresh_status not in {"success", "skipped", "timeout"}:
            result = {
                "status": "skipped",
                "return_code": None,
                "release_id": str(cycle_state.get("release_id", "") or ""),
                "warning_count": 0,
                "error_message": "research_refresh_not_ready",
                "stdout_log": "",
                "stderr_log": "",
                "stdout_tail": [],
                "stderr_tail": [],
                "result_status": "research_refresh_not_ready",
                "result_payload": {},
            }
            return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    elif phase_name in {"simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow"} or str(
        phase_name or ""
    ).startswith("intraday_tactical_"):
        if not str(cycle_state.get("release_id", "") or "").strip():
            result = {
                "status": "skipped",
                "return_code": None,
                "release_id": "",
                "warning_count": 0,
                "error_message": "no_formal_release_for_trade_date",
                "stdout_log": "",
                "stderr_log": "",
                "stdout_tail": [],
                "stderr_tail": [],
                "result_status": "no_formal_release",
                "result_payload": {},
            }
            return _mark_phase_complete(config, trade_date, profile, phase_name, result)
        if phase_name in {"afternoon_execution", "afternoon_shadow"}:
            midday_entry = dict(cycle_state.get("phases", {}).get("midday_review", {}) or {})
            midday_status = str(midday_entry.get("status", "") or "")
            midday_plan = _phase_execution_plan(config, cycle_state, phase_name)
            if midday_status != "success":
                result = {
                    "status": "skipped",
                    "return_code": None,
                    "release_id": str(cycle_state.get("release_id", "") or ""),
                    "warning_count": 0,
                    "error_message": "midday_review_not_ready",
                    "stdout_log": "",
                    "stderr_log": "",
                    "stdout_tail": [],
                    "stderr_tail": [],
                    "result_status": "midday_review_not_ready",
                    "result_payload": {},
                }
                return _mark_phase_complete(config, trade_date, profile, phase_name, result)
            if not bool(midday_plan.get("should_run", False)):
                result = {
                    "status": "skipped",
                    "return_code": None,
                    "release_id": str(midday_plan.get("release_id", "") or cycle_state.get("release_id", "") or ""),
                    "warning_count": 0,
                    "error_message": str(midday_plan.get("reason", "") or "midday_plan_skip"),
                    "stdout_log": "",
                    "stderr_log": "",
                    "stdout_tail": [],
                    "stderr_tail": [],
                    "result_status": "midday_plan_skip",
                    "result_payload": midday_plan,
                }
                return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    live_snapshot_refresh: Dict[str, Any] = {}
    if phase_name in {"preopen_gate", "simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow", "summary"} or str(
        phase_name or ""
    ).startswith("intraday_tactical_"):
        live_snapshot_refresh = _run_live_snapshot_refresh(config=config, trade_date=trade_date, phase_name=phase_name)
        if live_snapshot_refresh.get("ran", False) and not live_snapshot_refresh.get("ok", False) and not live_snapshot_refresh.get("fail_open", True):
            result = {
                "status": "failed",
                "return_code": None,
                "release_id": str(cycle_state.get("release_id", "") or ""),
                "warning_count": 1,
                "error_message": str(live_snapshot_refresh.get("message", "") or "live_snapshot_refresh_failed"),
                "stdout_log": "",
                "stderr_log": "",
                "stdout_tail": [],
                "stderr_tail": [],
                "result_status": "live_snapshot_refresh_failed",
                "result_payload": {"live_snapshot_refresh": live_snapshot_refresh},
            }
            return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    if phase_name == "summary":
        result = _run_summary_phase(config=config, trade_date=trade_date, profile=profile, cycle_state=cycle_state)
        if live_snapshot_refresh.get("ran", False):
            payload = dict(result.get("result_payload", {}) or {})
            payload["live_snapshot_refresh"] = live_snapshot_refresh
            result["result_payload"] = payload
            if not live_snapshot_refresh.get("ok", False):
                result["warning_count"] = int(result.get("warning_count", 0) or 0) + 1
        return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    if phase_name in {"research", "research_refresh"}:
        pre_research_refresh = run_pre_research_refresh_bundle(config=config, trade_date=trade_date)
        affordable_refresh = dict(pre_research_refresh.get("affordable_data_refresh", {}) or {})
        external_research_refresh = dict(pre_research_refresh.get("external_research_refresh", {}) or {})
        research_fact_refresh = dict(pre_research_refresh.get("research_fact_refresh", {}) or {})
        blocking_failure = dict(pre_research_refresh.get("blocking_failure", {}) or {})
        if blocking_failure:
            failed_name = str(blocking_failure.get("name", "") or "pre_research_refresh_failed")
            failed_payload = dict(blocking_failure.get("payload", {}) or {})
            result = {
                "status": "failed",
                "return_code": None,
                "release_id": "",
                "warning_count": int(pre_research_refresh.get("warning_count", 0) or 0),
                "error_message": str(blocking_failure.get("message", "") or failed_name),
                "stdout_log": str(failed_payload.get("stdout_log", "") or ""),
                "stderr_log": str(failed_payload.get("stderr_log", "") or ""),
                "stdout_tail": [],
                "stderr_tail": [],
                "result_status": failed_name,
                "result_payload": dict(pre_research_refresh or {}),
            }
            return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    data_readiness = assess_automation_data_readiness(config=config, trade_date=trade_date, phase_name=phase_name)
    gate_cfg = dict(_scheduler_cfg(config).get("data_consistency_gate", {}) or {})
    if data_readiness.get("enabled", False) and not data_readiness.get("ok", True) and not bool(gate_cfg.get("fail_open", False)):
        result = {
            "status": "failed",
            "return_code": None,
            "release_id": str(cycle_state.get("release_id", "") or ""),
            "warning_count": 1,
            "error_message": "data_consistency_gate_failed",
            "stdout_log": "",
            "stderr_log": "",
            "stdout_tail": [],
            "stderr_tail": [],
            "result_status": "data_consistency_gate_failed",
            "result_payload": {"data_consistency_gate": data_readiness},
        }
        return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    pre_intraday_state: Dict[str, Any] = {}
    if phase_name in _PHASES_REQUIRING_PRE_INTRADAY_REFRESH or str(phase_name or "").startswith("intraday_tactical_"):
        pre_intraday_state = _run_intraday_state_refresh(
            config=config,
            trade_date=trade_date,
            source_phase=phase_name,
            cycle_state=cycle_state,
        )
        ism_pre = _intraday_state_cfg(config)
        if bool(ism_pre.get("strict_pre_execution_gate", False)) and pre_intraday_state.get("ran") and not pre_intraday_state.get("ok", True):
            result = {
                "status": "skipped",
                "return_code": None,
                "release_id": str(cycle_state.get("release_id", "") or ""),
                "warning_count": 1,
                "error_message": str(pre_intraday_state.get("message", "") or "intraday_state_refresh_not_ok"),
                "stdout_log": "",
                "stderr_log": "",
                "stdout_tail": [],
                "stderr_tail": [],
                "result_status": "intraday_state_refresh_blocked",
                "result_payload": {"pre_intraday_state_refresh": pre_intraday_state},
            }
            return _mark_phase_complete(config, trade_date, profile, phase_name, result)
    raw = _subprocess_phase(
        config=config,
        trade_date=trade_date,
        phase_name=phase_name,
        command=_phase_command(config=config, phase_name=phase_name, profile=profile, trade_date=trade_date, cycle_state=cycle_state),
        timeout_minutes=specs[phase_name].timeout_minutes,
    )
    result = _normalise_phase_result(config, phase_name, trade_date, raw)
    if (
        (phase_name in _PHASES_REQUIRING_PRE_INTRADAY_REFRESH or str(phase_name or "").startswith("intraday_tactical_"))
        and pre_intraday_state.get("ran")
    ):
        payload = dict(result.get("result_payload", {}) or {})
        payload["pre_intraday_state_refresh"] = pre_intraday_state
        result["result_payload"] = payload
    if phase_name in {"research", "research_refresh"}:
        payload = dict(result.get("result_payload", {}) or {})
        payload.update(dict(pre_research_refresh or {}))
        payload["data_consistency_gate"] = data_readiness
        result["result_payload"] = payload
        result["warning_count"] = int(result.get("warning_count", 0) or 0) + int(pre_research_refresh.get("warning_count", 0) or 0)
    elif data_readiness.get("enabled", False):
        payload = dict(result.get("result_payload", {}) or {})
        payload["data_consistency_gate"] = data_readiness
        result["result_payload"] = payload
        if not data_readiness.get("ok", True):
            result["warning_count"] = int(result.get("warning_count", 0) or 0) + 1
    if live_snapshot_refresh.get("ran", False):
        payload = dict(result.get("result_payload", {}) or {})
        payload["live_snapshot_refresh"] = live_snapshot_refresh
        result["result_payload"] = payload
        if not live_snapshot_refresh.get("ok", False):
            result["warning_count"] = int(result.get("warning_count", 0) or 0) + 1
    return _mark_phase_complete(config, trade_date, profile, phase_name, result)


def _stop_requested(config: Dict[str, Any]) -> bool:
    return _stop_request_path(config).exists()


def _sleep_with_stop_check(config: Dict[str, Any], total_seconds: int) -> None:
    remaining = max(int(total_seconds or 0), 0)
    while remaining > 0:
        if _stop_requested(config):
            return
        step = 2 if remaining >= 2 else remaining
        time.sleep(step)
        remaining -= step


def _rapid_snapshot_loop_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    scheduler = _scheduler_cfg(config)
    return {
        "enabled": bool(scheduler.get("live_snapshot_loop_enabled", True)),
        "interval_seconds": max(int(scheduler.get("live_snapshot_loop_interval_seconds", 3) or 3), 1),
        "market_stages": {
            str(item or "").strip()
            for item in list(scheduler.get("live_snapshot_loop_market_stages", ["morning_session", "afternoon_session", "closing_auction"]) or [])
            if str(item or "").strip()
        },
        "fail_open": bool(scheduler.get("live_snapshot_refresh_fail_open", True)),
    }


def _scheduler_heartbeat_state(
    config: Dict[str, Any],
    profile: str,
    now: datetime,
) -> Dict[str, Any]:
    snapshot = trading_clock_snapshot(config=config, now=now)
    current_trade_date = _current_trade_date(config, now)
    next_trade_date = _next_trade_date(config, now.date())
    gate = assess_execution_gate(config=config, release_id="", ignore_window=False, now=now)
    safety = assess_system_safety(
        config=config,
        gate=gate,
        project_root=Path(__file__).resolve().parent.parent,
        service_name="trade_clock_service",
        current_mode="clock_scheduler",
        force_account_refresh=False,
    )
    due = _candidate_phases(config=config, profile=profile, now=now)
    next_due = due[0] if due else {}
    runtime_state = _load_json(_scheduler_runtime_state_path(config), default={})
    operator_guidance = _latest_portfolio_operator_guidance(config)
    state = {
        "last_heartbeat_at": str(snapshot.get("now", "") or now.isoformat(timespec="seconds")),
        "heartbeat_time": str(snapshot.get("now", "") or now.isoformat(timespec="seconds")),
        "market_stage": str(snapshot.get("market_stage", "") or ""),
        "calendar_ok": bool(snapshot.get("calendar_ok", False)),
        "is_trading_day": bool(snapshot.get("is_trading_day", False)),
        "current_trade_date": current_trade_date,
        "trade_date": current_trade_date,
        "next_trade_date": next_trade_date,
        "active_execution_window": snapshot.get("active_execution_window"),
        "gate": gate,
        "service_name": "trade_clock_service",
        "service_alive": True,
        "current_mode": "clock_scheduler",
        "scheduler_enabled": bool(_scheduler_cfg(config).get("enabled", True)),
        "scheduler_profile": str(profile or ""),
        "stop_requested": _stop_requested(config),
        "next_due_phase": str(next_due.get("phase_name", "") or ""),
        "next_due_trade_date": str(next_due.get("trade_date", "") or ""),
        "next_due_at": next_due.get("scheduled_at").isoformat(timespec="seconds") if next_due else "",
        "service_runtime_state_path": str(_scheduler_runtime_state_path(config)),
        "phase_state_root": str(_phase_state_root(config)),
        "locks_root": str(_locks_root(config)),
        "runtime_root": str(_runtime_root(config)),
        "automation_runs_root": str(_automation_runs_root(config)),
        "system_mode": str(safety.get("system_mode", "") or ""),
        "market_safety_regime": str(safety.get("market_safety_regime", "") or ""),
        "manual_halt": bool(safety.get("manual_halt", False)),
        "manual_reduce_only": bool(safety.get("manual_reduce_only", False)),
        "release_age_seconds": safety.get("state", {}).get("release_age_seconds"),
        "account_state_age_seconds": safety.get("state", {}).get("account_state_age_seconds"),
        "position_sync_age_seconds": safety.get("state", {}).get("position_sync_age_seconds"),
        "system_state_path": str(_trade_clock_root(config) / "system_safety_state.json"),
        "incident_log_path": str(_trade_clock_root(config) / "incident_log.jsonl"),
        "manual_overrides_path": str(_trade_clock_root(config) / "manual_overrides.json"),
        "runtime": runtime_state,
        "operator_guidance": operator_guidance,
    }
    account_snapshot = build_clock_account_snapshot(config)
    state["account_snapshot"] = account_snapshot
    return state


def run_trade_clock(
    config_path: Path,
    profile: str,
    poll_seconds: int | None = None,
    once: bool = False,
) -> Dict[str, Any]:
    config = load_config(config_path)
    _load_json._active_config = config
    _write_json._active_config = config
    sleep_seconds = int(poll_seconds if poll_seconds is not None else dict(config.get("trade_clock", {}) or {}).get("poll_seconds", 30) or 30)
    _sync_runtime_state(
        config=config,
        profile=profile,
        payload={
            "service_status": "starting",
            "config_path": str(config_path),
            "service_profile": str(profile or ""),
            "poll_seconds": int(sleep_seconds),
            "pid": os.getpid(),
            "active_phase": "",
            "active_trade_date": "",
            "last_exception": "",
            "stop_reason": "",
            "reload_reason": "",
            "reload_baseline_digest": "",
            "reload_current_digest": "",
            "reload_file_count": 0,
            "reload_latest_mtime_ns": 0,
        },
    )
    last_operator_runtime_publish_ts = 0.0
    last_live_snapshot_loop_ts = 0.0
    hot_reload = _hot_reload_cfg(config)
    last_hot_reload_check_ts = 0.0
    hot_reload_baseline = _runtime_watch_fingerprint(config, config_path) if bool(hot_reload.get("enabled", True)) and not once else {}
    while True:
        config = load_config(config_path)
        _load_json._active_config = config
        _write_json._active_config = config
        scheduler = _scheduler_cfg(config)
        rapid_snapshot_loop = _rapid_snapshot_loop_cfg(config)
        hot_reload = _hot_reload_cfg(config)
        now = clock_now(str(dict(config.get("trade_clock", {}) or {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        _clear_owned_locks(config)
        _reconcile_running_phases(config=config, profile=profile, now=now)
        heartbeat = _scheduler_heartbeat_state(config=config, profile=profile, now=now)
        current_trade_date = str(heartbeat.get("current_trade_date", "") or heartbeat.get("trade_date", "") or now.date().isoformat())
        acct = heartbeat.get("account_snapshot")
        if isinstance(acct, dict):
            _write_json(_trade_clock_root(config) / "clock_account_snapshot.json", acct)
        _write_json(_clock_state_path(config), heartbeat)
        if (
            bool(rapid_snapshot_loop.get("enabled", True))
            and heartbeat.get("market_stage") in rapid_snapshot_loop.get("market_stages", set())
            and (once or (time.time() - last_live_snapshot_loop_ts) >= max(int(rapid_snapshot_loop.get("interval_seconds", 3) or 3), 1))
        ):
            rapid_refresh = _run_live_snapshot_refresh(
                config=config,
                trade_date=current_trade_date,
                phase_name="rapid_loop",
                refresh_mode="rapid",
            )
            heartbeat["rapid_live_snapshot_refresh"] = rapid_refresh
            _write_json(_clock_state_path(config), heartbeat)
            if bool(rapid_refresh.get("ok", False)) or bool(rapid_snapshot_loop.get("fail_open", True)):
                last_live_snapshot_loop_ts = time.time()
            elif not bool(rapid_snapshot_loop.get("fail_open", True)):
                raise RuntimeError(str(rapid_refresh.get("message", "") or "rapid_live_snapshot_refresh_failed"))
        runtime_publish_cfg = _operator_runtime_publish_cfg(config)
        min_interval_seconds = int(runtime_publish_cfg.get("min_interval_seconds", 300) or 300)
        if bool(runtime_publish_cfg.get("enabled", True)) and (once or (time.time() - last_operator_runtime_publish_ts) >= max(min_interval_seconds, 30)):
            publish_result = _run_operator_runtime_publish(config=config, trade_date=current_trade_date)
            heartbeat["operator_runtime_publish"] = publish_result
            if bool(publish_result.get("ok", False)):
                heartbeat["remote_runtime_state_fresh"] = True
            elif publish_result.get("ran", False):
                heartbeat["service_status"] = "degraded_operator_runtime_publish"
                heartbeat["remote_runtime_state_fresh"] = False
            _write_json(_clock_state_path(config), heartbeat)
            if bool(publish_result.get("ok", False)):
                last_operator_runtime_publish_ts = time.time()
            elif not bool(publish_result.get("fail_open", True)):
                raise RuntimeError(str(publish_result.get("message", "") or "operator_runtime_publish_failed"))
        if _stop_requested(config):
            _sync_runtime_state(
                config=config,
                profile=profile,
                payload={
                    "service_status": "stopping",
                    "stop_reason": "manual_stop_request",
                    "active_phase": "",
                },
            )
            _clear_owned_locks(config)
            return heartbeat
        executed_phase = False
        if bool(scheduler.get("enabled", True)):
            due = _candidate_phases(config=config, profile=profile, now=now)
            if due:
                candidate = due[0]
                _sync_runtime_state(
                    config=config,
                    profile=profile,
                    payload={
                        "service_status": "running_phase",
                        "active_phase": str(candidate["phase_name"]),
                        "active_trade_date": str(candidate["trade_date"]),
                        "last_exception": "",
                    },
                )
                try:
                    state = _run_phase(
                        config=config,
                        profile=profile,
                        trade_date=str(candidate["trade_date"]),
                        phase_name=str(candidate["phase_name"]),
                        scheduled_at=candidate["scheduled_at"],
                    )
                except Exception as exc:
                    state = _mark_phase_exception(
                        config=config,
                        trade_date=str(candidate["trade_date"]),
                        profile=profile,
                        phase_name=str(candidate["phase_name"]),
                        exc=exc,
                    )
                    _sync_runtime_state(
                        config=config,
                        profile=profile,
                        payload={
                            "service_status": "phase_exception",
                            "active_phase": "",
                            "active_trade_date": "",
                            "last_phase": str(candidate["phase_name"]),
                            "last_trade_date": str(candidate["trade_date"]),
                            "last_phase_status": "failed",
                            "last_exception": str(exc),
                        },
                    )
                phase_bucket = dict(state.get("phases", {}).get(str(candidate["phase_name"]), {}) or {})
                phase_result_status = str(phase_bucket.get("result_status", "") or "")
                preserved_exception = str(phase_bucket.get("error_message", "") or "") if phase_result_status == "phase_exception" else ""
                _sync_runtime_state(
                    config=config,
                    profile=profile,
                    payload={
                        "service_status": "phase_exception" if phase_result_status == "phase_exception" else "idle",
                        "active_phase": "",
                        "active_trade_date": "",
                        "last_phase": str(candidate["phase_name"]),
                        "last_trade_date": str(candidate["trade_date"]),
                        "last_phase_status": str(phase_bucket.get("status", "") or ""),
                        "last_release_id": str(state.get("release_id", "") or ""),
                        "last_exception": preserved_exception,
                    },
                )
                executed_phase = True
        hot_reload_triggered = False
        hot_reload_reason = ""
        if bool(hot_reload.get("enabled", True)) and not once:
            check_interval_seconds = max(int(hot_reload.get("check_interval_seconds", 20) or 20), 5)
            if (time.time() - last_hot_reload_check_ts) >= check_interval_seconds:
                current_stamp = _runtime_watch_fingerprint(config, config_path)
                last_hot_reload_check_ts = time.time()
                if hot_reload_baseline and current_stamp.get("digest") != hot_reload_baseline.get("digest"):
                    hot_reload_triggered = True
                    hot_reload_reason = "runtime_source_changed"
                    _sync_runtime_state(
                        config=config,
                        profile=profile,
                        payload={
                            "service_status": "reload_requested",
                            "reload_reason": hot_reload_reason,
                            "reload_baseline_digest": str(hot_reload_baseline.get("digest", "") or ""),
                            "reload_current_digest": str(current_stamp.get("digest", "") or ""),
                            "reload_file_count": int(current_stamp.get("file_count", 0) or 0),
                            "reload_latest_mtime_ns": int(current_stamp.get("latest_mtime_ns", 0) or 0),
                        },
                    )
                else:
                    hot_reload_baseline = current_stamp
        print(
            json.dumps(
                {
                    "heartbeat": heartbeat.get("last_heartbeat_at", ""),
                    "market_stage": heartbeat.get("market_stage", ""),
                    "next_due_phase": heartbeat.get("next_due_phase", ""),
                    "next_due_trade_date": heartbeat.get("next_due_trade_date", ""),
                    "system_mode": heartbeat.get("system_mode", ""),
                    "market_regime": heartbeat.get("market_safety_regime", ""),
                    "stop_requested": heartbeat.get("stop_requested", False),
                    "phase_executed": executed_phase,
                    "hot_reload_triggered": hot_reload_triggered,
                },
                ensure_ascii=False,
            )
        )
        if hot_reload_triggered:
            raise RuntimeReloadRequested(hot_reload_reason or "runtime_source_changed")
        if once:
            return heartbeat
        effective_sleep_seconds = int(sleep_seconds)
        if bool(rapid_snapshot_loop.get("enabled", True)) and heartbeat.get("market_stage") in rapid_snapshot_loop.get("market_stages", set()):
            effective_sleep_seconds = min(effective_sleep_seconds, max(int(rapid_snapshot_loop.get("interval_seconds", 3) or 3), 1))
        _sleep_with_stop_check(config, max(int(effective_sleep_seconds or 0), 1))
