from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List


DEFAULT_TACTICAL_PHASE_SPECS: Dict[str, Dict[str, Any]] = {
    "intraday_tactical_0940": {"enabled": True, "time": "09:40:00", "timeout_minutes": 8},
    "intraday_tactical_1010": {"enabled": True, "time": "10:10:00", "timeout_minutes": 8},
    "intraday_tactical_1040": {"enabled": True, "time": "10:40:00", "timeout_minutes": 8},
    "intraday_tactical_1310": {"enabled": True, "time": "13:10:00", "timeout_minutes": 8},
    "intraday_tactical_1350": {"enabled": True, "time": "13:50:00", "timeout_minutes": 8},
    "intraday_tactical_1420": {"enabled": True, "time": "14:20:00", "timeout_minutes": 8},
}

BASE_PHASE_SEQUENCE = [
    "research",
    "release",
    "research_refresh",
    "release_refresh",
    "preopen_gate",
    "simulation",
    "shadow",
    "midday_review",
    "afternoon_execution",
    "afternoon_shadow",
    "summary",
]


def _parse_hms(value: str) -> datetime.time:
    return datetime.strptime(str(value or "00:00:00"), "%H:%M:%S").time()


def tactical_phase_map(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    tactics_cfg = dict(config.get("intraday_tactics", {}) or {})
    phases = dict(tactics_cfg.get("scheduler_phases", {}) or {})
    if not phases:
        phases = dict(DEFAULT_TACTICAL_PHASE_SPECS)
    return phases


def tactical_phase_names(config: Dict[str, Any], *, enabled_only: bool = False) -> List[str]:
    rows = []
    for name, row in tactical_phase_map(config).items():
        payload = dict(row or {})
        if enabled_only and not bool(payload.get("enabled", True)):
            continue
        rows.append((str(payload.get("time", "23:59:59") or "23:59:59"), str(name)))
    rows.sort(key=lambda item: (_parse_hms(item[0]), item[1]))
    return [name for _, name in rows]


def phase_sequence(config: Dict[str, Any]) -> List[str]:
    names = tactical_phase_names(config, enabled_only=False)
    morning = []
    afternoon = []
    for name in names:
        row = dict(tactical_phase_map(config).get(name, {}) or {})
        if _parse_hms(str(row.get("time", "23:59:59") or "23:59:59")) < _parse_hms("12:00:00"):
            morning.append(name)
        else:
            afternoon.append(name)
    return [
        "research",
        "release",
        "research_refresh",
        "release_refresh",
        "preopen_gate",
        "simulation",
        "shadow",
        *morning,
        "midday_review",
        "afternoon_execution",
        *afternoon,
        "afternoon_shadow",
        "summary",
    ]
