from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def load_oms_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(config.get("oms", {}) or {})
    default_root = Path(str(config.get("paths", {}).get("live_execution_root", "") or "")).resolve() / "oms_v1"
    return {
        "enabled": bool(raw.get("enabled", True)),
        "output_root": str(Path(str(raw.get("output_root", default_root) or default_root)).resolve()),
        "use_broker_truth_for_v2a_continuity": bool(raw.get("use_broker_truth_for_v2a_continuity", True)),
        "intent_expiry_days": max(int(raw.get("intent_expiry_days", 3) or 3), 1),
        "control_feedback_lookback_runs": max(int(raw.get("control_feedback_lookback_runs", 20) or 20), 5),
        "research_meta_lookback_runs": max(int(raw.get("research_meta_lookback_runs", 60) or 60), 10),
        "compat_write_latest_account_state": bool(raw.get("compat_write_latest_account_state", True)),
        "enable_broker_cancel": bool(raw.get("enable_broker_cancel", True)),
    }
