from __future__ import annotations

import copy
from typing import Any, Dict

from . import local_settings as LS


ALLOWED_RUNTIME_PROFILES = ("quick_test", "daily_production", "overnight")


def normalize_profile(profile: str) -> str:
    raw = str(profile or "").strip().lower()
    if raw in ALLOWED_RUNTIME_PROFILES:
        return raw
    fallback = str(getattr(LS, "DEFAULT_RUN_PROFILE", "quick_test") or "quick_test").strip().lower()
    return fallback if fallback in ALLOWED_RUNTIME_PROFILES else "quick_test"


def profile_overrides(profile: str) -> Dict[str, Dict[str, Any]]:
    resolved = normalize_profile(profile)
    if resolved == "quick_test":
        return {
            "supervisor": {
                "gpu_research_max_cycles_per_tick": int(getattr(LS, "QUICK_TEST_GPU_RESEARCH_MAX_CYCLES_PER_TICK", 1) or 1),
                "token_plan_min_interval_hours": float(getattr(LS, "QUICK_TEST_TOKEN_PLAN_MIN_INTERVAL_HOURS", 24) or 24),
            },
            "event_ingest": {
                "max_pdf_fetch_per_run": int(getattr(LS, "QUICK_TEST_MAX_PDF_FETCH_PER_RUN", 2) or 2),
            },
            "event_extract": {
                "max_events_per_run": int(getattr(LS, "QUICK_TEST_MAX_EVENTS_PER_RUN", 6) or 6),
                "batch_size": int(getattr(LS, "QUICK_TEST_DEEPSEEK_BATCH_SIZE", 2) or 2),
            },
            "research_context_pack": {
                "max_priority_events": int(getattr(LS, "QUICK_TEST_MAX_PRIORITY_EVENTS", 4) or 4),
            },
        }
    if resolved == "daily_production":
        return {
            "supervisor": {
                "gpu_research_max_cycles_per_tick": int(getattr(LS, "DAILY_PRODUCTION_GPU_RESEARCH_MAX_CYCLES_PER_TICK", 3) or 3),
                "token_plan_min_interval_hours": float(getattr(LS, "DAILY_PRODUCTION_TOKEN_PLAN_MIN_INTERVAL_HOURS", 24) or 24),
            },
            "event_ingest": {
                "max_pdf_fetch_per_run": int(getattr(LS, "DAILY_PRODUCTION_MAX_PDF_FETCH_PER_RUN", 6) or 6),
            },
            "event_extract": {
                "max_events_per_run": int(getattr(LS, "DAILY_PRODUCTION_MAX_EVENTS_PER_RUN", 12) or 12),
                "batch_size": int(getattr(LS, "DAILY_PRODUCTION_DEEPSEEK_BATCH_SIZE", 4) or 4),
            },
            "research_context_pack": {
                "max_priority_events": int(getattr(LS, "DAILY_PRODUCTION_MAX_PRIORITY_EVENTS", 8) or 8),
            },
        }
    return {
        "supervisor": {
            "gpu_research_max_cycles_per_tick": int(getattr(LS, "OVERNIGHT_GPU_RESEARCH_MAX_CYCLES_PER_TICK", 8) or 8),
            "token_plan_min_interval_hours": float(getattr(LS, "OVERNIGHT_TOKEN_PLAN_MIN_INTERVAL_HOURS", 24) or 24),
        },
        "event_ingest": {
            "max_pdf_fetch_per_run": int(getattr(LS, "OVERNIGHT_MAX_PDF_FETCH_PER_RUN", 12) or 12),
        },
        "event_extract": {
            "max_events_per_run": int(getattr(LS, "OVERNIGHT_MAX_EVENTS_PER_RUN", 18) or 18),
            "batch_size": int(getattr(LS, "OVERNIGHT_DEEPSEEK_BATCH_SIZE", 6) or 6),
        },
        "research_context_pack": {
            "max_priority_events": int(getattr(LS, "OVERNIGHT_MAX_PRIORITY_EVENTS", 12) or 12),
        },
    }


def _deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in updates.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def deep_merge_runtime_config(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    if not updates:
        return base
    out = copy.deepcopy(base)
    return _deep_merge(out, updates)


