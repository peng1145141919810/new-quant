from __future__ import annotations

from typing import Any, Dict, List


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_reason_chain(row: Dict[str, Any]) -> List[str]:
    thesis_chain = list(row.get("thesis_reason_chain", []) or [])
    if thesis_chain:
        return [_text(item) for item in thesis_chain if _text(item)]
    out: List[str] = []
    event_type = _text(row.get("primary_event_type"))
    mechanism = _text(row.get("primary_mechanism_group"))
    earnings_reason = _text(row.get("earnings_reason"))
    if event_type:
        out.append(f"event:{event_type}")
    if mechanism:
        out.append(f"mechanism:{mechanism}")
    if earnings_reason:
        out.append(f"earnings:{earnings_reason}")
    if bool(row.get("is_research_proxy_involved")):
        out.append("proxy_input_involved")
    return out


def build_explainer_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    top = rows[:12]
    return {
        "top_candidates": [
            {
                "symbol": _text(item.get("symbol")),
                "integrated_thesis_score": item.get("integrated_thesis_score", 0.0),
                "integrated_thesis_state": _text(item.get("integrated_thesis_state")),
                "primary_reason_chain": list(item.get("primary_reason_chain", []) or []),
                "primary_event_type": _text(item.get("primary_event_type")),
                "primary_mechanism_group": _text(item.get("primary_mechanism_group")),
                "primary_event_fact_id": _text(item.get("primary_event_fact_id")),
                "mechanism_reason_chain": list(item.get("mechanism_reason_chain", []) or []),
                "earnings_reason_chain": list(item.get("earnings_reason_chain", []) or []),
                "thesis_reason_chain": list(item.get("thesis_reason_chain", []) or []),
                "thesis_gate_stage": _text(item.get("thesis_gate_stage")),
                "thesis_reject_reason": _text(item.get("thesis_reject_reason")),
            }
            for item in top
        ]
    }
