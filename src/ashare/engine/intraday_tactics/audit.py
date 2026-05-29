from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..config_utils import ensure_dir


def write_tactical_audit_jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def build_latest_audit_summary(
    *,
    trade_date: str,
    tactical_phase: str,
    n_intents: int,
    n_orders: int,
    reason_counts: Dict[str, int],
    audit_root: Path,
) -> Path:
    latest = ensure_dir(audit_root / "latest")
    payload = {
        "trade_date": trade_date,
        "tactical_phase": tactical_phase,
        "generated_at": "",
        "n_intents": n_intents,
        "n_tactical_orders": n_orders,
        "reason_code_counts": reason_counts,
    }
    out = latest / "latest_intraday_tactical_audit.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
