from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import pandas as pd

from ..config_utils import ensure_dir
from ..research_fact_store import load_best_symbol_event_facts
from .contracts import STATE_THRESHOLDS
from .earnings_validator import build_earnings_validation
from .event_gate import build_event_cards
from .explainer import build_explainer_payload, build_reason_chain
from .mechanism_join import build_mechanism_candidates


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _normalize_integrated_thesis_trade_date(raw: Any) -> str:
    """Normalize to YYYYMMDD string so CSV history + new rows sort and dedupe consistently."""
    t = _text(raw)
    if not t:
        return datetime.now().strftime("%Y%m%d")
    if t.isdigit() and len(t) == 8:
        return t
    parsed = pd.to_datetime(t, errors="coerce")
    if pd.isna(parsed):
        digits = "".join(ch for ch in t if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else datetime.now().strftime("%Y%m%d")
    return parsed.strftime("%Y%m%d")


def _output_root(config: Dict[str, Any]) -> Path:
    cfg = dict(config.get("integrated_thesis", {}) or {})
    configured = _text(cfg.get("output_root"))
    if configured:
        return ensure_dir(Path(configured).resolve())
    research_root = Path(str(config.get("paths", {}).get("research_root", "") or "")).resolve()
    return ensure_dir(research_root / "integrated_thesis")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _state(score: float) -> str:
    if score >= STATE_THRESHOLDS["build"]:
        return "build"
    if score >= STATE_THRESHOLDS["pilot"]:
        return "pilot"
    if score >= STATE_THRESHOLDS["watch"]:
        return "watch"
    return "reject"


def _router_top_signals(industry_router_payload: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    payload = dict(industry_router_payload or {})
    return [dict(item) for item in list(payload.get("top_stock_signals", []) or []) if isinstance(item, dict)]


def _fact_event_quality(fact: Dict[str, Any]) -> float:
    if not fact:
        return 0.0
    importance = {
        "critical": 1.0,
        "high": 0.82,
        "medium": 0.60,
        "low": 0.38,
    }.get(_text(fact.get("importance_level")).lower(), 0.40)
    source_confidence = _clip(_float(fact.get("source_confidence", 0.0)))
    major_bonus = 0.08 if bool(_float(fact.get("is_major_event", 0.0)) > 0) else 0.0
    return _clip(importance * 0.62 + source_confidence * 0.30 + major_bonus)


def _candidate_symbols(joined: Sequence[Dict[str, Any]], router_signals: Sequence[Dict[str, Any]]) -> List[str]:
    wanted: List[str] = []
    for item in list(joined) + list(router_signals):
        symbol = _text(item.get("symbol") or item.get("ts_code")).upper()
        if symbol and symbol not in wanted:
            wanted.append(symbol)
    return wanted


def _fact_backed_candidates(
    *,
    router_signals: Sequence[Dict[str, Any]],
    fact_map: Dict[str, Dict[str, Any]],
    existing_pairs: Iterable[tuple[str, str]],
) -> List[Dict[str, Any]]:
    seen = set(existing_pairs)
    out: List[Dict[str, Any]] = []
    for signal in list(router_signals)[:18]:
        symbol = _text(signal.get("symbol") or signal.get("ts_code")).upper()
        fact = dict(fact_map.get(symbol, {}) or {})
        if not symbol or not fact:
            continue
        key = (_text(fact.get("fact_id")) or f"fact::{symbol}", symbol)
        if key in seen:
            continue
        seen.add(key)
        mechanism_group = _text(signal.get("mechanism_primary") or signal.get("mechanism_group") or fact.get("mechanism_hint") or "research_fact")
        out.append(
            {
                "symbol": symbol,
                "event_id": _text(fact.get("fact_id")) or f"fact::{symbol}",
                "mechanism_group": mechanism_group,
                "mechanism_fit_score": round(
                    _clip(
                        0.30
                        + _float(signal.get("final_score", signal.get("signal_score", 0.0))) * 0.42
                        + (0.10 if _text(fact.get("mechanism_hint")).lower() in mechanism_group.lower() else 0.0)
                    ),
                    4,
                ),
                "mechanism_state_score": round(_clip(_float(signal.get("final_score", signal.get("signal_score", 0.0)))), 4),
                "signal_state": _text(signal.get("signal_state") or "fact_router_join"),
                "allow_entry_from_router": bool(signal.get("allow_entry", True)),
                "router_reason": "research_fact_router_join",
                "event_quality": round(_fact_event_quality(fact), 4),
                "primary_event_type": _text(fact.get("event_type")),
                "event_summary": _text(fact.get("headline") or fact.get("project_name") or fact.get("notes"))[:220],
            }
        )
    for symbol, fact in fact_map.items():
        key = (_text(fact.get("fact_id")) or f"fact::{symbol}", symbol)
        if key in seen:
            continue
        mechanism_group = _text(fact.get("mechanism_hint"))
        if not symbol or not mechanism_group:
            continue
        seen.add(key)
        out.append(
            {
                "symbol": symbol,
                "event_id": _text(fact.get("fact_id")) or f"fact::{symbol}",
                "mechanism_group": mechanism_group,
                "mechanism_fit_score": 0.42,
                "mechanism_state_score": 0.22,
                "signal_state": "fact_only",
                "allow_entry_from_router": True,
                "router_reason": "research_fact_direct_mapping",
                "event_quality": round(_fact_event_quality(fact), 4),
                "primary_event_type": _text(fact.get("event_type")),
                "event_summary": _text(fact.get("headline") or fact.get("project_name") or fact.get("notes"))[:220],
            }
        )
    return out


def _event_gate(item: Dict[str, Any], fact: Dict[str, Any]) -> Dict[str, Any]:
    direction = _text(fact.get("direction") if fact else item.get("direction")).lower()
    event_strength = max(_float(item.get("event_quality", 0.0)), _fact_event_quality(fact))
    fact_confidence = _clip(_float(fact.get("source_confidence", 0.0))) if fact else 0.0
    if direction == "negative":
        return {
            "pass": False,
            "stage": "event_gate",
            "event_strength": round(event_strength, 4),
            "fact_confidence": round(fact_confidence, 4),
            "reject_reason": "negative_event_direction",
            "reason_chain": [
                f"event_type:{_text(fact.get('event_type') or item.get('primary_event_type') or 'unknown')}",
                "event_direction:negative",
            ],
        }
    passed = bool(event_strength >= 0.30)
    reason_chain = [
        f"event_type:{_text(fact.get('event_type') or item.get('primary_event_type') or 'unknown')}",
        f"event_strength:{event_strength:.3f}",
    ]
    if fact:
        reason_chain.append(f"fact_source:{_text(fact.get('source_class') or 'research_fact')}")
        reason_chain.append(f"fact_confidence:{fact_confidence:.3f}")
    return {
        "pass": passed,
        "stage": "event_gate_passed" if passed else "event_gate",
        "event_strength": round(event_strength, 4),
        "fact_confidence": round(fact_confidence, 4),
        "reject_reason": "" if passed else "event_strength_too_weak",
        "reason_chain": reason_chain,
    }


def _mechanism_gate(item: Dict[str, Any], fact: Dict[str, Any]) -> Dict[str, Any]:
    fit = _float(item.get("mechanism_fit_score", 0.0))
    state = _float(item.get("mechanism_state_score", 0.0))
    allow_entry = bool(item.get("allow_entry_from_router", True))
    mechanism_group = _text(item.get("mechanism_group") or fact.get("mechanism_hint") or "unknown")
    hint_match = bool(_text(fact.get("mechanism_hint")).lower() and _text(fact.get("mechanism_hint")).lower() in mechanism_group.lower())
    alignment_bonus = 0.08 if hint_match else 0.0
    mechanism_strength = _clip(0.56 * min(1.0, fit + alignment_bonus) + 0.44 * max(state, 0.0))
    passed = bool(allow_entry and (fit + alignment_bonus) >= 0.30 and state >= 0.08)
    reason = ""
    if not allow_entry:
        reason = "router_blocks_new_entry"
    elif fit + alignment_bonus < 0.30:
        reason = "mechanism_transmission_not_clear"
    elif state < 0.08:
        reason = "mechanism_state_not_supportive"
    return {
        "pass": passed,
        "mechanism_strength": round(mechanism_strength, 4),
        "reject_reason": reason,
        "reason_chain": [
            f"mechanism_group:{mechanism_group}",
            f"mechanism_fit:{fit + alignment_bonus:.3f}",
            f"mechanism_state:{state:.3f}",
            f"router_allow_entry:{str(allow_entry).lower()}",
            *(["fact_hint_match"] if hint_match else []),
        ],
    }


def _portfolio_gate(market_state_payload: Dict[str, Any] | None) -> Dict[str, Any]:
    payload = dict(market_state_payload or {})
    risk_budget = _float(payload.get("risk_budget_multiplier", 1.0), 1.0)
    new_position_policy = _text(payload.get("new_position_policy") or "allow").lower()
    passed = bool(risk_budget >= 0.35 and new_position_policy not in {"block", "reduce_only"})
    reason = ""
    if not passed:
        reason = "market_state_blocks_new_risk" if new_position_policy in {"block", "reduce_only"} else "risk_budget_too_low"
    return {
        "pass": passed,
        "risk_budget": round(risk_budget, 4),
        "reject_reason": reason,
        "reason_chain": [
            f"risk_budget:{risk_budget:.3f}",
            f"new_position_policy:{new_position_policy or 'allow'}",
        ],
    }


def _confidence_adjustment(
    *,
    event_gate: Dict[str, Any],
    mechanism_gate: Dict[str, Any],
    earnings: Dict[str, Any],
    research_proxy: bool,
) -> float:
    return round(
        _clip(
            0.56
            + _float(event_gate.get("fact_confidence", 0.0)) * 0.16
            + _float(mechanism_gate.get("mechanism_strength", 0.0)) * 0.10
            + _float(earnings.get("earnings_confidence", 0.0)) * 0.12
            + _float(earnings.get("revision_support", 0.0)) * 0.08
            - _float(earnings.get("implementation_risk", 0.0)) * 0.18
            - (0.10 if research_proxy else 0.0),
            0.25,
            1.0,
        ),
        4,
    )


def _integrated_score(
    *,
    event_gate: Dict[str, Any],
    mechanism_gate: Dict[str, Any],
    earnings: Dict[str, Any],
    portfolio_gate: Dict[str, Any],
    confidence_adjustment: float,
    passes_all: bool,
) -> float:
    base = _clip(
        _float(event_gate.get("event_strength", 0.0)) * 0.30
        + _float(mechanism_gate.get("mechanism_strength", 0.0)) * 0.30
        + _float(earnings.get("earnings_validation_score", 0.0)) * 0.28
        + min(1.0, _float(portfolio_gate.get("risk_budget", 1.0))) * 0.12
    )
    adjusted = _clip(base * (0.78 + 0.22 * confidence_adjustment))
    if not passes_all:
        return round(min(adjusted, STATE_THRESHOLDS["watch"] - 0.02), 4)
    if (
        _float(event_gate.get("event_strength", 0.0)) >= 0.72
        and _float(mechanism_gate.get("mechanism_strength", 0.0)) >= 0.58
        and _float(earnings.get("earnings_validation_score", 0.0)) >= 0.46
    ):
        adjusted = max(adjusted, STATE_THRESHOLDS["pilot"] + 0.02)
    return round(max(adjusted, STATE_THRESHOLDS["watch"] + 0.02), 4)


def _thesis_gate_outcome(
    *,
    event_gate: Dict[str, Any],
    mechanism_gate: Dict[str, Any],
    earnings: Dict[str, Any],
    portfolio_gate: Dict[str, Any],
) -> tuple[str, str]:
    if not bool(event_gate.get("pass")):
        return "event_gate", _text(event_gate.get("reject_reason"))
    if not bool(mechanism_gate.get("pass")):
        return "mechanism_gate", _text(mechanism_gate.get("reject_reason"))
    if not bool(earnings.get("earnings_gate_pass")):
        return "earnings_soft_gate", ""
    if not bool(portfolio_gate.get("pass")):
        return "portfolio_soft_gate", _text(portfolio_gate.get("reject_reason"))
    return "portfolio_gate_passed", ""


def build_integrated_thesis_artifacts(
    config: Dict[str, Any],
    *,
    structured_events: List[Dict[str, Any]],
    industry_router_payload: Dict[str, Any] | None = None,
    market_state_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    output_root = _output_root(config)
    latest_path = output_root / "integrated_thesis_state.json"
    daily_path = output_root / "integrated_thesis_daily.csv"
    candidates_path = output_root / "integrated_thesis_candidates.csv"
    explainer_path = output_root / "integrated_thesis_explainer.json"
    latest_table_path = output_root / "latest_integrated_thesis.csv"

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade_date = _normalize_integrated_thesis_trade_date(
        _text(dict(market_state_payload or {}).get("date")) or generated_at[:10]
    )
    event_cards = build_event_cards(structured_events or [])
    joined = build_mechanism_candidates(event_cards, industry_router_payload)
    router_signals = _router_top_signals(industry_router_payload)

    fact_map = load_best_symbol_event_facts(
        config,
        symbols=_candidate_symbols(joined, router_signals),
        as_of_date=trade_date,
        lookback_days=60,
    )
    extra_candidates = _fact_backed_candidates(
        router_signals=router_signals,
        fact_map=fact_map,
        existing_pairs={(_text(item.get("event_id")), _text(item.get("symbol")).upper()) for item in joined},
    )
    candidates = list(joined) + list(extra_candidates)
    earnings = build_earnings_validation(config=config, symbols=[item.get("symbol", "") for item in candidates])

    rows: List[Dict[str, Any]] = []
    best_by_symbol: Dict[str, Dict[str, Any]] = {}
    portfolio_gate = _portfolio_gate(market_state_payload)
    for item in candidates:
        symbol = _text(item.get("symbol")).upper()
        if not symbol:
            continue
        fact = dict(fact_map.get(symbol, {}) or {})
        earn = dict(earnings.get(symbol, {}) or {})
        event_gate = _event_gate(item, fact)
        mechanism_gate = _mechanism_gate(item, fact)
        stage, reject_reason = _thesis_gate_outcome(
            event_gate=event_gate,
            mechanism_gate=mechanism_gate,
            earnings=earn,
            portfolio_gate=portfolio_gate,
        )
        research_proxy = bool(item.get("is_research_proxy")) or "proxy" in _text(fact.get("source_class")).lower()
        confidence_adjustment = _confidence_adjustment(
            event_gate=event_gate,
            mechanism_gate=mechanism_gate,
            earnings=earn,
            research_proxy=research_proxy,
        )
        passes_core = bool(event_gate.get("pass")) and bool(mechanism_gate.get("pass"))
        passes_admission = stage in {"portfolio_gate_passed", "earnings_soft_gate", "portfolio_soft_gate"}
        integrated_score = _integrated_score(
            event_gate=event_gate,
            mechanism_gate=mechanism_gate,
            earnings=earn,
            portfolio_gate=portfolio_gate,
            confidence_adjustment=confidence_adjustment,
            passes_all=passes_admission,
        )
        row = {
            "trade_date": trade_date,
            "symbol": symbol,
            "ts_code": symbol,
            "event_quality": round(_float(event_gate.get("event_strength", item.get("event_quality", 0.0))), 4),
            "mechanism_fit_score": round(_float(item.get("mechanism_fit_score", 0.0)), 4),
            "mechanism_state_score": round(_float(item.get("mechanism_state_score", 0.0)), 4),
            "earnings_validation_score": round(_float(earn.get("earnings_validation_score", 0.0)), 4),
            "confidence_adjustment": confidence_adjustment,
            "integrated_thesis_score": integrated_score,
            "integrated_thesis_state": _state(integrated_score if passes_core else 0.0),
            "primary_event_type": _text(fact.get("event_type") or item.get("primary_event_type")),
            "primary_mechanism_group": _text(item.get("mechanism_group") or fact.get("mechanism_hint")),
            "primary_event_fact_id": _text(fact.get("fact_id") or item.get("event_id")),
            "primary_reason_chain": [],
            "mechanism_reason_chain": list(mechanism_gate.get("reason_chain", []) or []),
            "earnings_reason_chain": list(earn.get("earnings_reason_chain", []) or []),
            "thesis_reason_chain": [],
            "thesis_gate_stage": stage,
            "thesis_reject_reason": reject_reason,
            "event_gate_pass": bool(event_gate.get("pass")),
            "mechanism_gate_pass": bool(mechanism_gate.get("pass")),
            "earnings_gate_pass": bool(earn.get("earnings_gate_pass")),
            "portfolio_gate_pass": bool(portfolio_gate.get("pass")),
            "is_research_proxy_involved": research_proxy,
            "earnings_reason": _text(earn.get("earnings_reason")),
            "event_summary": _text(item.get("event_summary") or fact.get("headline")),
            "source_mix": _text(earn.get("source_mix")),
            "valuation_penalty": round(_float(earn.get("valuation_penalty", 0.0)), 4),
            "implementation_risk": round(_float(earn.get("implementation_risk", 0.0)), 4),
            "event_source_class": _text(fact.get("source_class")),
            "event_source_confidence": round(_float(fact.get("source_confidence", 0.0)), 4),
        }
        thesis_chain = (
            list(event_gate.get("reason_chain", []) or [])
            + list(mechanism_gate.get("reason_chain", []) or [])
            + list(earn.get("earnings_reason_chain", []) or [])
            + list(portfolio_gate.get("reason_chain", []) or [])
        )
        if row["thesis_gate_stage"] not in {"portfolio_gate_passed", "earnings_soft_gate", "portfolio_soft_gate"}:
            thesis_chain.append(f"reject_reason:{row['thesis_reject_reason']}")
        row["thesis_reason_chain"] = thesis_chain
        row["primary_reason_chain"] = build_reason_chain(row)
        rows.append(row)
        prev = best_by_symbol.get(symbol)
        if prev is None or _float(row.get("integrated_thesis_score", 0.0)) > _float(prev.get("integrated_thesis_score", 0.0)):
            best_by_symbol[symbol] = row

    rows = sorted(best_by_symbol.values(), key=lambda item: float(item.get("integrated_thesis_score", 0.0)), reverse=True)
    accepted = [item for item in rows if _text(item.get("integrated_thesis_state")) != "reject"]
    soft_admitted = [
        item for item in rows
        if _text(item.get("thesis_gate_stage")) in {"earnings_soft_gate", "portfolio_soft_gate"}
    ]
    top_candidates = accepted[:12] if accepted else rows[:8]
    alpha_budget_multiplier = round(
        _clip(
            0.70
            + min(len(accepted), 8) / 8.0 * 0.10
            + sum(_float(item.get("integrated_thesis_score", 0.0)) for item in top_candidates[:5]) / max(len(top_candidates[:5]), 1) * 0.20,
            0.70,
            1.00,
        ),
        4,
    )

    payload = {
        "generated_at": generated_at,
        "trade_date": trade_date,
        "status": "ok",
        "formal_strategy_framework": "adaptive_multi_alpha_control",
        "primary_strategy_key": "multi_alpha_allocator",
        "portfolio_construction": {
            "alpha_budget_multiplier": alpha_budget_multiplier,
            "risk_budget_reference": round(_float(dict(market_state_payload or {}).get("risk_budget_multiplier", 1.0), 1.0), 4),
        },
        "summary": {
            "n_event_cards": len(event_cards),
            "n_joined_candidates": len(joined),
            "n_fact_backed_candidates": len(extra_candidates),
            "n_symbols": len(rows),
            "n_accepted": len(accepted),
            "n_soft_admitted": len(soft_admitted),
            "top_candidate_count": len(top_candidates),
            "event_gate_pass_count": sum(1 for item in rows if bool(item.get("event_gate_pass"))),
            "mechanism_gate_pass_count": sum(1 for item in rows if bool(item.get("mechanism_gate_pass"))),
            "earnings_gate_pass_count": sum(1 for item in rows if bool(item.get("earnings_gate_pass"))),
            "portfolio_gate_pass_count": sum(1 for item in rows if bool(item.get("portfolio_gate_pass"))),
            "event_type_distribution": dict(Counter(_text(item.get("primary_event_type")) for item in rows)),
            "mechanism_distribution": dict(Counter(_text(item.get("primary_mechanism_group")) for item in rows)),
            "event_source_distribution": dict(Counter(_text(item.get("event_source_class")) or "missing" for item in rows)),
            "gate_stage_distribution": dict(Counter(_text(item.get("thesis_gate_stage")) or "unknown" for item in rows)),
        },
        "top_candidates": top_candidates,
        "artifacts": {
            "latest_path": str(latest_path),
            "daily_path": str(daily_path),
            "candidates_path": str(candidates_path),
            "latest_table_path": str(latest_table_path),
            "explainer_path": str(explainer_path),
        },
    }
    _write_json(latest_path, payload)
    _write_json(explainer_path, build_explainer_payload(rows))
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(
            columns=[
                "trade_date",
                "symbol",
                "ts_code",
                "integrated_thesis_score",
                "integrated_thesis_state",
                "primary_event_type",
                "primary_mechanism_group",
                "primary_event_fact_id",
                "primary_reason_chain",
                "mechanism_reason_chain",
                "earnings_reason_chain",
                "thesis_reason_chain",
                "thesis_gate_stage",
                "thesis_reject_reason",
            ]
        )
    frame.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    frame.to_csv(latest_table_path, index=False, encoding="utf-8-sig")
    daily_row = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "generated_at": generated_at,
                "n_symbols": len(rows),
                "n_accepted": len(accepted),
                "n_event_cards": len(event_cards),
                "alpha_budget_multiplier": alpha_budget_multiplier,
                "top_integrated_score": _float(top_candidates[0].get("integrated_thesis_score", 0.0)) if top_candidates else 0.0,
            }
        ]
    )
    if daily_path.exists():
        history = pd.read_csv(daily_path)
        daily_row = pd.concat([history, daily_row], ignore_index=True)
    daily_row["trade_date"] = daily_row["trade_date"].map(_normalize_integrated_thesis_trade_date)
    daily_row = daily_row.drop_duplicates(subset=["trade_date"], keep="last").sort_values("trade_date")
    daily_row.to_csv(daily_path, index=False, encoding="utf-8-sig")
    return {
        "ok": True,
        "status": "ok",
        "latest_path": str(latest_path),
        "daily_path": str(daily_path),
        "candidates_path": str(candidates_path),
        "payload": payload,
    }


def load_latest_integrated_thesis_state(config: Dict[str, Any], allow_build: bool = False) -> Dict[str, Any]:
    path = _output_root(config) / "integrated_thesis_state.json"
    if path.exists():
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return {}
    if allow_build:
        return {}
    return {}
