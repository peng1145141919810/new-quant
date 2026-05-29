from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Iterable, List


DEFAULT_TIMING_WINDOWS: List[Dict[str, Any]] = [
    {
        "name": "open_noise_window",
        "start": "09:30:00",
        "end": "09:40:00",
        "allow_new_entry": False,
        "allow_build_entry": False,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": False,
        "allow_t_second_leg": False,
        "allow_reconcile": True,
        "allow_aggressive": False,
    },
    {
        "name": "morning_primary_window",
        "start": "09:40:00",
        "end": "10:30:00",
        "allow_new_entry": True,
        "allow_build_entry": True,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": True,
        "allow_t_second_leg": False,
        "allow_reconcile": True,
        "allow_aggressive": True,
    },
    {
        "name": "mid_morning_low_speed_window",
        "start": "10:30:00",
        "end": "11:20:00",
        "allow_new_entry": False,
        "allow_build_entry": False,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": False,
        "allow_t_second_leg": False,
        "allow_reconcile": True,
        "allow_aggressive": False,
    },
    {
        "name": "afternoon_primary_window",
        "start": "13:00:00",
        "end": "14:20:00",
        "allow_new_entry": False,
        "allow_build_entry": True,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": False,
        "allow_t_second_leg": True,
        "allow_reconcile": True,
        "allow_aggressive": True,
    },
    {
        "name": "late_afternoon_reconcile_window",
        "start": "14:20:00",
        "end": "14:50:00",
        "allow_new_entry": False,
        "allow_build_entry": False,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": False,
        "allow_t_second_leg": True,
        "allow_reconcile": True,
        "allow_aggressive": False,
    },
    {
        "name": "post_1450_close_only_window",
        "start": "14:50:00",
        "end": "15:00:00",
        "allow_new_entry": False,
        "allow_build_entry": False,
        "allow_trim": True,
        "allow_exit": True,
        "allow_t_first_leg": False,
        "allow_t_second_leg": False,
        "allow_reconcile": True,
        "allow_aggressive": False,
    },
]


def _parse_hms(value: Any) -> time:
    text = str(value or "00:00:00").strip() or "00:00:00"
    hh, mm, ss = [int(part) for part in text.split(":")]
    return time(hour=hh, minute=mm, second=ss)


def _window_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    intraday_cfg = dict(config.get("intraday_state_machine", {}) or {})
    timing_cfg = dict(intraday_cfg.get("timing_layer", {}) or {})
    raw_windows = timing_cfg.get("window_config", []) or []
    if isinstance(raw_windows, dict):
        windows: List[Dict[str, Any]] = []
        for name, payload in raw_windows.items():
            item = dict(payload or {}) if isinstance(payload, dict) else {}
            item.setdefault("name", str(name or "").strip())
            windows.append(item)
        return windows or list(DEFAULT_TIMING_WINDOWS)
    windows = list(raw_windows)
    return windows or list(DEFAULT_TIMING_WINDOWS)


def _normalize_window(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row or {})
    payload["name"] = str(payload.get("name", "") or "").strip() or "unnamed_window"
    payload["start"] = str(payload.get("start", "00:00:00") or "00:00:00")
    payload["end"] = str(payload.get("end", "00:00:00") or "00:00:00")
    for key in (
        "allow_new_entry",
        "allow_build_entry",
        "allow_trim",
        "allow_exit",
        "allow_t_first_leg",
        "allow_t_second_leg",
        "allow_reconcile",
        "allow_aggressive",
    ):
        payload[key] = bool(payload.get(key, False))
    return payload


def _iter_windows(config: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for row in _window_config(config):
        yield _normalize_window(row)


def _find_window(config: Dict[str, Any], window_name: str) -> Dict[str, Any]:
    needle = str(window_name or "").strip()
    for row in _iter_windows(config):
        if row["name"] == needle:
            return row
    return {}


def resolve_timing_window(
    *,
    config: Dict[str, Any],
    now_dt: datetime,
    phase_name: str,
) -> Dict[str, Any]:
    current_t = now_dt.time()
    for row in _iter_windows(config):
        start_t = _parse_hms(row["start"])
        end_t = _parse_hms(row["end"])
        if start_t <= current_t < end_t:
            payload = dict(row)
            payload["active"] = True
            payload["phase_name"] = str(phase_name or "")
            payload["time_text"] = now_dt.strftime("%H:%M:%S")
            return payload
    fallback_name = "midday_break_window" if str(phase_name or "") == "midday_review" else "out_of_session"
    return {
        "name": fallback_name,
        "start": "",
        "end": "",
        "allow_new_entry": False,
        "allow_build_entry": False,
        "allow_trim": str(phase_name or "") in {"close_reconcile", "postclose_archive"},
        "allow_exit": str(phase_name or "") in {"close_reconcile", "postclose_archive"},
        "allow_t_first_leg": False,
        "allow_t_second_leg": False,
        "allow_reconcile": True,
        "allow_aggressive": False,
        "active": False,
        "phase_name": str(phase_name or ""),
        "time_text": now_dt.strftime("%H:%M:%S"),
    }


def projected_window(config: Dict[str, Any], window_name: str) -> Dict[str, Any]:
    row = _find_window(config, window_name)
    if not row:
        return {"name": str(window_name or ""), "active": False}
    payload = dict(row)
    payload["active"] = False
    payload["phase_name"] = "projection"
    payload["time_text"] = ""
    return payload
