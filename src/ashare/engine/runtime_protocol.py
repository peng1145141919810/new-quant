from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, Iterable, List

PROTOCOL_VERSION = "scheduler.v1"
ARTIFACT_IDENTITY_VERSION = "artifact_identity.v1"
ADVICE_VERSION = "advice.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stable_join(parts: Iterable[Any]) -> str:
    return "|".join(_text(part) for part in parts)


def build_lineage_token(
    *,
    run_id: str,
    trade_date: str,
    release_id: str,
    phase: str,
    producer: str,
) -> str:
    seed = _stable_join([run_id, trade_date, release_id, phase, producer])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"ln_{digest}"


def artifact_identity(
    *,
    run_id: str = "",
    trade_date: str = "",
    release_id: str = "",
    phase: str = "",
    producer: str = "",
    generated_at: str = "",
    schema_version: str = "1",
    lineage_token: str = "",
    parent_lineage_token: str = "",
) -> Dict[str, Any]:
    normalized_run_id = _text(run_id)
    normalized_trade_date = _text(trade_date)
    normalized_release_id = _text(release_id)
    normalized_phase = _text(phase)
    normalized_producer = _text(producer)
    normalized_generated_at = _text(generated_at) or _iso_now()
    normalized_lineage = _text(lineage_token) or build_lineage_token(
        run_id=normalized_run_id,
        trade_date=normalized_trade_date,
        release_id=normalized_release_id,
        phase=normalized_phase,
        producer=normalized_producer,
    )
    return {
        "protocol_version": ARTIFACT_IDENTITY_VERSION,
        "run_id": normalized_run_id,
        "trade_date": normalized_trade_date,
        "release_id": normalized_release_id,
        "phase": normalized_phase,
        "producer": normalized_producer,
        "generated_at": normalized_generated_at,
        "schema_version": _text(schema_version) or "1",
        "lineage_token": normalized_lineage,
        "parent_lineage_token": _text(parent_lineage_token),
    }


def release_artifact_identity(release_doc: Dict[str, Any], *, producer: str = "portfolio_release") -> Dict[str, Any]:
    existing = dict(release_doc.get("artifact_identity", {}) or {})
    return artifact_identity(
        run_id=_text(existing.get("run_id") or release_doc.get("run_id")),
        trade_date=_text(existing.get("trade_date") or release_doc.get("trade_date")),
        release_id=_text(existing.get("release_id") or release_doc.get("release_id")),
        phase=_text(existing.get("phase") or "release_manifest"),
        producer=_text(existing.get("producer") or producer),
        generated_at=_text(existing.get("generated_at") or release_doc.get("generated_at")),
        schema_version=_text(existing.get("schema_version") or release_doc.get("schema_version") or "1"),
        lineage_token=_text(existing.get("lineage_token")),
        parent_lineage_token=_text(existing.get("parent_lineage_token")),
    )


def build_advice(
    *,
    advisor: str,
    status: str,
    summary: str,
    category: str = "advisory",
    score: float | None = None,
    hard_stop: bool = False,
    reasons: Iterable[Any] | None = None,
    evidence: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "protocol_version": ADVICE_VERSION,
        "advisor": _text(advisor),
        "category": _text(category) or "advisory",
        "status": _text(status) or "ok",
        "summary": _text(summary),
        "hard_stop": bool(hard_stop),
        "reasons": [_text(reason) for reason in list(reasons or []) if _text(reason)],
        "evidence": dict(evidence or {}),
        "payload": dict(payload or {}),
    }
    if score is not None:
        item["score"] = round(float(score), 6)
    return item


def compact_reason_chain(advice_items: Iterable[Dict[str, Any]]) -> List[str]:
    reasons: List[str] = []
    for item in advice_items:
        advisor = _text(item.get("advisor"))
        for reason in list(item.get("reasons", []) or []):
            normalized = _text(reason)
            if not normalized:
                continue
            tagged = f"{advisor}:{normalized}" if advisor else normalized
            if tagged not in reasons:
                reasons.append(tagged)
    return reasons

