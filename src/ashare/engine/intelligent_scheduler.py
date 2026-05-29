from __future__ import annotations

from typing import Any, Dict, List

from .runtime_protocol import PROTOCOL_VERSION, artifact_identity, build_advice, compact_reason_chain, release_artifact_identity

FINAL_VERDICTS = {"proceed", "proceed_degraded", "reduce_only", "defer", "block"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    return bool(value)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _default_release_context(release_doc: Dict[str, Any]) -> Dict[str, Any]:
    identity = release_artifact_identity(release_doc, producer="intelligent_scheduler.release")
    return {
        "release_id": _text(release_doc.get("release_id")),
        "trade_date": _text(release_doc.get("trade_date")),
        "run_id": _text(release_doc.get("run_id") or identity.get("run_id")),
        "artifact_identity": identity,
    }


def _advisor_chain(
    *,
    gate: Dict[str, Any],
    safety: Dict[str, Any],
    brain_decision: Any,
    llm_review: Dict[str, Any],
    trade_discipline: Dict[str, Any],
    operating_brain: Dict[str, Any],
    global_objective: Dict[str, Any],
    harvest_risk: Dict[str, Any],
    econometric_guardrails: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    gate_reason = _text(gate.get("reason"))
    gate_release = dict(gate.get("release", {}) or {})
    items.append(
        build_advice(
            advisor="execution_gate",
            category="gate",
            status="ok" if _bool(gate.get("should_execute")) else "defer",
            summary=gate_reason or ("execution_window_open" if _bool(gate.get("should_execute")) else "execution_window_closed"),
            hard_stop=not _bool(gate.get("ok", False)),
            reasons=[gate_reason] if gate_reason else [],
            evidence={
                "should_execute": _bool(gate.get("should_execute")),
                "trade_date_ok": _bool(gate.get("trade_date_ok")),
                "time_window_ok": _bool(gate.get("time_window_ok")),
                "release_id": _text(gate_release.get("release_id")),
                "trade_date": _text(gate_release.get("trade_date")),
            },
        )
    )
    safety_reasons = []
    if not _bool(safety.get("allow_execution", True)):
        safety_reasons.append(_text(safety.get("halt_reason")) or "safety_disallow_execution")
    if _text(safety.get("system_mode")).upper() == "HALT":
        safety_reasons.append("system_halt")
    items.append(
        build_advice(
            advisor="safety_guard",
            category="risk",
            status="block" if safety_reasons else "ok",
            summary=_text(safety.get("market_safety_regime")) or _text(safety.get("system_mode")) or "safety_normal",
            hard_stop=bool(safety_reasons),
            reasons=safety_reasons,
            evidence={
                "allow_execution": _bool(safety.get("allow_execution", True)),
                "system_mode": _text(safety.get("system_mode")),
                "market_safety_regime": _text(safety.get("market_safety_regime")),
                "broker_reachable": _bool(safety.get("broker_reachable", True)),
            },
        )
    )
    llm_payload = dict(llm_review.get("review", {}) or {})
    items.append(
        build_advice(
            advisor="execution_llm_review",
            category="advisor",
            status="warning" if _bool(llm_payload.get("reduce_only")) else "ok" if _bool(llm_review.get("applied", False)) else "inactive",
            summary=_text(llm_payload.get("review_summary")) or ("llm_unavailable" if not _bool(llm_review.get("applied", False)) else "llm_review_ready"),
            reasons=list(llm_payload.get("risk_flags", []) or []),
            evidence={
                "applied": _bool(llm_review.get("applied", False)),
                "risk_level": _text(llm_payload.get("risk_level")),
                "turnover_multiplier": _float(llm_payload.get("turnover_multiplier"), 1.0),
                "reduce_only": _bool(llm_payload.get("reduce_only", False)),
            },
            payload=llm_payload,
        )
    )
    trade_summary = _text(trade_discipline.get("posture")) or "trade_discipline_unavailable"
    items.append(
        build_advice(
            advisor="trade_discipline",
            category="advisor",
            status="warning" if trade_summary in {"defensive", "reduce_only"} else "ok",
            summary=trade_summary,
            reasons=list(trade_discipline.get("sell_priority_symbols", []) or [])[:3],
            evidence={
                "posture": _text(trade_discipline.get("posture")),
                "sell_pressure": _float(trade_discipline.get("sell_pressure")),
                "add_multiplier": _float(trade_discipline.get("add_multiplier"), 1.0),
            },
        )
    )
    operating_review = dict(operating_brain.get("review", operating_brain) or {})
    dispatch_brain = dict(operating_review.get("dispatch_brain", {}) or {})
    items.append(
        build_advice(
            advisor="llm_operating_brain",
            category="advisor",
            status="ok" if _bool(operating_review.get("applied", False)) else "inactive",
            summary=_text(dispatch_brain.get("preferred_posture")) or "llm_operating_brain_unavailable",
            reasons=list(operating_review.get("uncertainty_flags", []) or []),
            evidence={
                "applied": _bool(operating_review.get("applied", False)),
                "preferred_posture": _text(dispatch_brain.get("preferred_posture")),
                "cash_posture": _text(dispatch_brain.get("cash_posture")),
                "tactical_bias": _text(dispatch_brain.get("tactical_bias")),
            },
        )
    )
    objective_scores = dict(global_objective.get("scores", {}) or {})
    objective_flags = list(global_objective.get("hard_flags", []) or [])
    objective_posture = _text(global_objective.get("policy_posture")) or "balanced"
    items.append(
        build_advice(
            advisor="global_objective",
            category="arbitration",
            status="warning" if objective_flags else "ok",
            summary=objective_posture,
            reasons=objective_flags,
            evidence={
                "overall_score": _float(objective_scores.get("overall"), 0.0),
                "evidence_score": _float(objective_scores.get("evidence"), 0.0),
                "diversity_score": _float(objective_scores.get("diversity"), 0.0),
                "execution_score": _float(objective_scores.get("execution"), 0.0),
                "adversarial_score": _float(objective_scores.get("adversarial"), 0.0),
            },
            payload=global_objective,
        )
    )
    harvest_tags = list(harvest_risk.get("tags", []) or [])
    harvest_score = _float(harvest_risk.get("harvest_risk_score"), 0.0)
    items.append(
        build_advice(
            advisor="harvest_risk",
            category="risk",
            status="warning" if harvest_score >= 0.55 else "ok",
            summary=_text(harvest_risk.get("harvest_risk_level")) or "unknown",
            reasons=harvest_tags,
            evidence={
                "harvest_risk_score": harvest_score,
                "confidence": _float(harvest_risk.get("harvest_risk_confidence"), 0.0),
                "dominant_family": _text(harvest_risk.get("dominant_family")),
                "dominant_family_share": _float(harvest_risk.get("dominant_family_share"), 0.0),
            },
            payload=harvest_risk,
        )
    )
    items.append(
        build_advice(
            advisor="econometric_guardrails",
            category="risk",
            status="warning" if _text(econometric_guardrails.get("stability_flag")) in {"warning", "fragile"} else "ok",
            summary=_text(econometric_guardrails.get("stability_flag")) or "unknown",
            reasons=[
                f"guardrail_penalty={_float(econometric_guardrails.get('guardrail_penalty'), 0.0):.3f}",
                f"spurious_correlation_risk={_float(econometric_guardrails.get('spurious_correlation_risk'), 0.0):.3f}",
            ],
            evidence={
                "stability_score": _float(econometric_guardrails.get("stability_score"), 0.0),
                "guardrail_penalty": _float(econometric_guardrails.get("guardrail_penalty"), 0.0),
                "regime_dependency_score": _float(econometric_guardrails.get("regime_dependency_score"), 0.0),
                "incremental_value_score": _float(econometric_guardrails.get("incremental_value_score"), 0.0),
            },
            payload=econometric_guardrails,
        )
    )
    dimensions = []
    verdict = "proceed"
    summary = "constraint_brain_unavailable"
    blocked_symbols: List[str] = []
    favored_symbols: List[str] = []
    turnover_multiplier = 1.0
    size_multiplier = 1.0
    reduce_only = False
    if brain_decision is not None:
        verdict = _text(getattr(brain_decision, "verdict", "")) or "proceed"
        summary = _text(getattr(brain_decision, "summary", "constraint_brain_ready"))
        blocked_symbols = list(getattr(brain_decision, "blocked_symbols", []) or [])
        favored_symbols = list(getattr(brain_decision, "favored_symbols", []) or [])
        turnover_multiplier = _float(getattr(brain_decision, "turnover_multiplier", 1.0), 1.0)
        size_multiplier = _float(getattr(brain_decision, "size_multiplier", 1.0), 1.0)
        reduce_only = _bool(getattr(brain_decision, "reduce_only", False))
        for dimension in list(getattr(brain_decision, "dimensions", []) or []):
            dimensions.append(
                {
                    "name": _text(getattr(dimension, "name", "")),
                    "verdict": _text(getattr(dimension, "verdict", "")),
                    "reason": _text(getattr(dimension, "reason", "")),
                    "score": _float(getattr(dimension, "score", 0.0), 0.0),
                }
            )
    items.append(
        build_advice(
            advisor="constraint_brain",
            category="arbitration",
            status=verdict if verdict in FINAL_VERDICTS else "warning",
            summary=summary,
            hard_stop=verdict == "block",
            reasons=[item.get("reason", "") for item in dimensions if _text(item.get("reason"))],
            evidence={
                "verdict": verdict,
                "reduce_only": reduce_only,
                "turnover_multiplier": turnover_multiplier,
                "size_multiplier": size_multiplier,
                "blocked_symbols": blocked_symbols,
                "favored_symbols": favored_symbols,
            },
            payload={"dimensions": dimensions},
        )
    )
    return items


def build_execution_scheduler_verdict(
    *,
    gate: Dict[str, Any],
    safety: Dict[str, Any],
    release_doc: Dict[str, Any],
    market_state: Dict[str, Any],
    account_snapshot: Dict[str, Any],
    llm_review: Dict[str, Any],
    brain_decision: Any,
    trade_discipline: Dict[str, Any],
    operating_brain: Dict[str, Any],
    global_objective: Dict[str, Any] | None = None,
    harvest_risk: Dict[str, Any] | None = None,
    econometric_guardrails: Dict[str, Any] | None = None,
    trigger_label: str,
    trigger_source: str,
    intent_source: str,
    execution_namespace: str,
    shadow_run: bool,
) -> Dict[str, Any]:
    release_context = _default_release_context(release_doc)
    release_identity = release_context["artifact_identity"]
    objective_payload = dict(global_objective or {})
    harvest_payload = dict(harvest_risk or {})
    guardrail_payload = dict(econometric_guardrails or {})
    scheduler_identity = artifact_identity(
        run_id=_text(release_identity.get("run_id")),
        trade_date=_text(release_identity.get("trade_date")),
        release_id=_text(release_identity.get("release_id")),
        phase="execution_scheduler",
        producer="intelligent_scheduler",
        parent_lineage_token=_text(release_identity.get("lineage_token")),
    )
    advice_chain = _advisor_chain(
        gate=gate,
        safety=safety,
        brain_decision=brain_decision,
        llm_review=llm_review,
        trade_discipline=trade_discipline,
        operating_brain=operating_brain,
        global_objective=objective_payload,
        harvest_risk=harvest_payload,
        econometric_guardrails=guardrail_payload,
    )
    reasons = compact_reason_chain(advice_chain)
    brain_verdict = _text(getattr(brain_decision, "verdict", "")) or "proceed"
    objective_flags = set(list(objective_payload.get("hard_flags", []) or []))
    objective_scores = dict(objective_payload.get("scores", {}) or {})
    guardrail_penalty = _float(guardrail_payload.get("guardrail_penalty"), 0.0)
    recommended_budget = dict(objective_payload.get("recommended_budget", {}) or {})
    if not _bool(gate.get("ok", False)):
        final_verdict = "block"
    elif not _bool(safety.get("allow_execution", True)) and not shadow_run:
        final_verdict = "block"
    elif shadow_run:
        final_verdict = brain_verdict if brain_verdict in FINAL_VERDICTS else "proceed_degraded"
    elif not _bool(gate.get("should_execute", False)):
        final_verdict = "defer"
    elif {"execution_below_floor", "guardrail_penalty_above_ceiling"} & objective_flags:
        final_verdict = "reduce_only"
    elif {"harvest_risk_above_ceiling", "incremental_value_below_floor"} & objective_flags:
        final_verdict = "proceed_degraded"
    elif bool(recommended_budget.get("shadow_only", False)):
        final_verdict = "proceed_degraded"
    elif brain_verdict in FINAL_VERDICTS:
        final_verdict = brain_verdict
    elif _float(objective_scores.get("overall"), 0.0) < 0.42 or guardrail_penalty >= 0.60:
        final_verdict = "proceed_degraded"
    else:
        final_verdict = "proceed"
    reduce_only = _bool(getattr(brain_decision, "reduce_only", False))
    blocked_symbols = list(getattr(brain_decision, "blocked_symbols", []) or [])
    favored_symbols = list(getattr(brain_decision, "favored_symbols", []) or [])
    turnover_multiplier = _float(getattr(brain_decision, "turnover_multiplier", 1.0), 1.0)
    size_multiplier = _float(getattr(brain_decision, "size_multiplier", 1.0), 1.0)
    if final_verdict == "proceed_degraded":
        turnover_multiplier = min(turnover_multiplier, 0.75)
        size_multiplier = min(size_multiplier, 0.80)
    if final_verdict == "reduce_only":
        turnover_multiplier = min(turnover_multiplier, 0.60)
        size_multiplier = min(size_multiplier, 0.50)
    should_dispatch = final_verdict in {"proceed", "proceed_degraded", "reduce_only"} or shadow_run
    execution_allowed = should_dispatch and final_verdict != "block"
    status = {
        "proceed": "ready",
        "proceed_degraded": "ready_degraded",
        "reduce_only": "ready_reduce_only",
        "defer": "deferred",
        "block": "blocked",
    }[final_verdict]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "authority_owner": "intelligent_scheduler",
        "artifact_identity": scheduler_identity,
        "release_identity": release_identity,
        "final_verdict": final_verdict,
        "status": status,
        "execution_allowed": execution_allowed,
        "should_dispatch": should_dispatch,
        "shadow_run": bool(shadow_run),
        "execution_namespace": _text(execution_namespace) or "main",
        "trigger": {
            "label": _text(trigger_label) or "manual",
            "source": _text(trigger_source) or "manual",
            "intent_source": _text(intent_source) or "release",
        },
        "reason_chain": reasons,
        "advisor_chain": advice_chain,
        "global_objective": objective_payload,
        "harvest_risk": harvest_payload,
        "econometric_guardrails": guardrail_payload,
        "execution_plan": {
            "reduce_only": reduce_only or final_verdict == "reduce_only",
            "turnover_multiplier": turnover_multiplier,
            "size_multiplier": size_multiplier,
            "blocked_symbols": blocked_symbols,
            "favored_symbols": favored_symbols,
            "market_regime": _text(market_state.get("market_regime")),
            "new_position_policy": _text(market_state.get("new_position_policy")),
            "policy_posture": _text(objective_payload.get("policy_posture")),
        },
        "context_snapshot": {
            "release_id": release_context["release_id"],
            "trade_date": release_context["trade_date"],
            "run_id": release_context["run_id"],
            "account_id": _text(account_snapshot.get("account_id")),
            "system_mode": _text(safety.get("system_mode")),
            "market_safety_regime": _text(safety.get("market_safety_regime")),
        },
    }
