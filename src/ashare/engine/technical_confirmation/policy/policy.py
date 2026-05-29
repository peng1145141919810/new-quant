from __future__ import annotations

from typing import Any, Dict


def summarize_technical_frame(frame, strictness: float) -> Dict[str, Any]:
    if frame is None or getattr(frame, "empty", True):
        return {
            "rows": 0,
            "allow_count": 0,
            "reject_count": 0,
            "wait_count": 0,
            "existing_position_count": 0,
            "strictness": float(strictness or 0.0),
        }
    gate_reason_counts = frame["tech_gate_reason"].astype(str).value_counts().to_dict() if "tech_gate_reason" in frame.columns else {}
    entry_style_counts = frame["tech_entry_style"].astype(str).value_counts().to_dict() if "tech_entry_style" in frame.columns else {}
    return {
        "rows": int(len(frame.index)),
        "allow_count": int(frame["tech_allow_entry"].astype(bool).sum()) if "tech_allow_entry" in frame.columns else 0,
        "reject_count": int((~frame["tech_allow_entry"].astype(bool)).sum()) if "tech_allow_entry" in frame.columns else 0,
        "wait_count": int((frame["tech_entry_style"].astype(str) == "wait").sum()) if "tech_entry_style" in frame.columns else 0,
        "existing_position_count": int(frame["is_existing_position"].astype(bool).sum()) if "is_existing_position" in frame.columns else 0,
        "strictness": float(strictness or 0.0),
        "gate_reason_counts": gate_reason_counts,
        "entry_style_counts": entry_style_counts,
    }
