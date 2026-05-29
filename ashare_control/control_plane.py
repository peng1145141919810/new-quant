from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


@dataclass(frozen=True)
class DataSourceDescriptor:
    key: str
    kind: str
    latency: str
    truth_class: str
    role: str
    status: str


@dataclass(frozen=True)
class AlphaDescriptor:
    key: str
    horizon: str
    posture: str
    source_mix: list[str]
    objective: str
    status: str


@dataclass
class ControlPlaneSnapshot:
    generated_at: str
    focus_mode: str
    market: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    industry: dict[str, Any] = field(default_factory=dict)
    automation: dict[str, Any] = field(default_factory=dict)
    intelligence: dict[str, Any] = field(default_factory=dict)
    data_sources: list[dict[str, Any]] = field(default_factory=list)
    alpha_stack: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def runtime_config_alias_paths(config_dir: Path, profile: str) -> list[Path]:
    return [
        config_dir / f"hub_config.v6.runtime.{profile}.json",
        config_dir / f"runtime_config.{profile}.json",
    ]


def runtime_stage_preview(mode: str, config: dict[str, Any]) -> list[str]:
    market_enabled = bool(config.get("market_pipeline", {}).get("enabled", False))
    release_enabled = bool(config.get("portfolio_recommendation", {}).get("enabled", False))
    execution_enabled = bool(config.get("execution_bridge", {}).get("enabled", False))

    if mode == "integrated_supervisor":
        stages: list[str] = []
        if market_enabled:
            stages.append("Market and fact refresh")
        stages.extend(["Signal planning", "Cross-source research cycle"])
        if release_enabled:
            stages.append("Portfolio release")
        if bool(config.get("evidence_audit", {}).get("run_after_portfolio_recommendation", False)):
            stages.append("Small-pool evidence audit")
        if execution_enabled:
            stages.append("Execution bridge")
        return stages

    mapping = {
        "research_only": ["Market and fact refresh", "Signal planning", "Cross-source research cycle", "Portfolio release"],
        "release_only": ["Portfolio release"],
        "execution_only": ["Release intake", "Safety gate", "Execution bridge"],
        "midday_review_only": ["OMS truth intake", "Gap analysis", "Midday adjustment plan"],
        "resume_downstream": ["Portfolio release recovery", "Optional execution continuation"],
        "intraday_tactics_only": ["Intraday context load", "Tactical trigger scan", "Intent and order artifact write"],
        "evidence_audit_only": ["Candidate pool load", "Web/source fetch", "LLM evidence audit", "Evidence artifact write"],
        "oms_validate": ["OMS replay validation", "Continuity checks", "Validation artifacts"],
        "full_cycle": ["Ingest", "Extract", "Industry routing", "Risk state", "Gap analysis", "Signal planning", "Bridge artifacts"],
        "ingest_only": ["Ingest"],
        "extract_only": ["Ingest", "Extract"],
        "gap_only": ["Ingest", "Extract", "Gap analysis"],
        "industry_router_only": ["Industry routing", "Daily signal materialization"],
        "plan_only": ["Ingest", "Extract", "Industry routing", "Risk state", "Gap analysis", "Signal planning"],
        "bridge_only": ["Ingest", "Extract", "Industry routing", "Risk state", "Gap analysis", "Signal planning", "Bridge artifacts"],
    }
    return mapping.get(mode, ["Unknown stage"])


def _default_data_sources() -> list[dict[str, Any]]:
    return [
        asdict(DataSourceDescriptor("exchange_disclosure", "official", "daily", "issuer_disclosure_truth", "price, filings, official notices", "active")),
        asdict(DataSourceDescriptor("tushare_affordable_bundle", "vendor", "daily", "derived_from_truth", "low-cost market and ownership refresh", "active")),
        asdict(DataSourceDescriptor("qianzhan_industry_watch", "web", "daily", "derived_from_truth", "industry chain, expansion and capex clues", "active")),
        asdict(DataSourceDescriptor("public_procurement_events", "official", "daily", "official_truth", "contract/order event facts", "active")),
        asdict(DataSourceDescriptor("customs_macro_digest", "official", "daily", "official_truth", "export and demand proxies", "active")),
        asdict(DataSourceDescriptor("intraday_proxy_stream", "market", "intraday", "derived_from_truth", "live quotes, account truth and tactical state", "active")),
        asdict(DataSourceDescriptor("news_flash_intelligence", "planned", "intraday", "research_only", "突发消息与主题催化聚合入口", "planned")),
        asdict(DataSourceDescriptor("industry_policy_graph", "planned", "daily", "research_only", "产业链政策关系图谱", "planned")),
    ]


def _default_alpha_stack() -> list[dict[str, Any]]:
    return [
        asdict(AlphaDescriptor("event_drive", "swing", "aggressive", ["exchange_disclosure", "public_procurement_events"], "event follow-through", "live")),
        asdict(AlphaDescriptor("order_flow", "short", "aggressive", ["public_procurement_events", "tushare_affordable_bundle"], "contract and order acceleration", "live")),
        asdict(AlphaDescriptor("revision", "swing", "balanced", ["exchange_disclosure", "tushare_affordable_bundle"], "expectation repricing", "live")),
        asdict(AlphaDescriptor("industry", "swing", "balanced", ["qianzhan_industry_watch", "customs_macro_digest"], "industry diffusion", "shadow")),
        asdict(AlphaDescriptor("valuation", "medium", "balanced", ["tushare_affordable_bundle"], "valuation reversion", "shadow")),
        asdict(AlphaDescriptor("liquidity", "intraday", "flexible", ["intraday_proxy_stream"], "intraday liquidity timing", "shadow")),
    ]


def _first_json(paths: list[Path]) -> dict[str, Any]:
    for path in paths:
        payload = _read_json(path)
        if payload:
            return payload
    return {}


def build_control_plane_snapshot(
    repo_root: Path,
    *,
    runtime_context_path: Path | None = None,
    site_state_path: Path | None = None,
) -> dict[str, Any]:
    site_state_candidates: list[Path] = []
    if isinstance(site_state_path, Path):
        site_state_candidates.append(site_state_path)
    site_state_candidates.extend([
        repo_root / "outputs" / "site_publish_stage" / "site_state.json",
        repo_root / "site_portal" / "site_state.json",
    ])
    runtime_context_candidates: list[Path] = []
    if isinstance(runtime_context_path, Path):
        runtime_context_candidates.append(runtime_context_path)
    runtime_context_candidates.extend([
        repo_root / "outputs" / "site_publish_stage" / "operator_runtime_context.json",
        repo_root / "site_portal" / "operator_runtime_context.json",
    ])
    site_state = _first_json(site_state_candidates)
    runtime_context = _first_json(runtime_context_candidates)

    account = runtime_context.get("account", {}) or {}
    safety = runtime_context.get("safety", {}) or {}
    market_state = runtime_context.get("market_state", {}) or {}
    thesis = runtime_context.get("integrated_thesis_summary", {}) or {}
    construction = runtime_context.get("integrated_thesis_portfolio_construction", {}) or {}
    alpha_lifecycle = runtime_context.get("alpha_lifecycle", {}) or {}
    llm_operating_brain = runtime_context.get("llm_operating_brain", {}) or {}
    notes = list(runtime_context.get("notes", []) or [])

    portfolio = {
        "trade_date": runtime_context.get("trade_date", "-"),
        "release_id": runtime_context.get("release_id", site_state.get("latest_release_id", "-")),
        "cash": account.get("cash"),
        "nav": account.get("nav"),
        "positions_count": len(runtime_context.get("positions", []) or []),
        "target_count": site_state.get("target_count", 0),
        "position_count": site_state.get("position_count", 0),
        "discipline_posture": dict(runtime_context.get("trade_discipline", {}) or {}).get("posture", ""),
        "cash_posture": dict(runtime_context.get("trade_discipline", {}) or {}).get("cash_posture", ""),
    }
    industry = {
        "event_cards": thesis.get("n_event_cards", 0),
        "joined_candidates": thesis.get("n_joined_candidates", 0),
        "accepted_candidates": thesis.get("n_accepted", 0),
        "event_type_distribution": thesis.get("event_type_distribution", {}),
        "mechanism_distribution": thesis.get("mechanism_distribution", {}),
    }
    automation = {
        "clock_phase": runtime_context.get("clock_phase", "-"),
        "clock_mode": runtime_context.get("clock_mode", "-"),
        "run_id": runtime_context.get("run_id", "-"),
        "report_count": site_state.get("report_count", 0),
        "heartbeat_at": runtime_context.get("heartbeat_at", site_state.get("generated_at", "-")),
    }
    intelligence = {
        "risk_budget_multiplier": market_state.get("risk_budget_multiplier"),
        "alpha_budget_multiplier": construction.get("alpha_budget_multiplier"),
        "market_regime": market_state.get("market_regime", "unknown"),
        "style_bias": market_state.get("style_bias", "unknown"),
        "mechanism_bias": market_state.get("mechanism_bias", "unknown"),
        "market_snapshot_reason": safety.get("market_snapshot", {}).get("reason", ""),
        "alpha_promote_families": list(alpha_lifecycle.get("promote_families", []) or []),
        "alpha_demote_families": list(alpha_lifecycle.get("demote_families", []) or []),
        "dispatch_posture": ((llm_operating_brain.get("review", {}) or {}).get("dispatch_brain", {}) or {}).get("preferred_posture", ""),
        "tactical_bias": ((llm_operating_brain.get("review", {}) or {}).get("dispatch_brain", {}) or {}).get("tactical_bias", ""),
        "trade_discipline": dict(runtime_context.get("trade_discipline", {}) or {}),
    }
    snapshot = ControlPlaneSnapshot(
        generated_at=_now_iso(),
        focus_mode="aggressive_adaptive",
        market=market_state,
        portfolio=portfolio,
        safety={
            "system_mode": safety.get("system_mode", "unknown"),
            "gate_status": safety.get("current_gate_status", "unknown"),
            "gate_open": safety.get("gate_open"),
            "halt_reason": safety.get("halt_reason", ""),
            "market_safety_regime": safety.get("market_safety_regime", "unknown"),
            "account_health": safety.get("account_health", {}),
        },
        industry=industry,
        automation=automation,
        intelligence=intelligence,
        data_sources=_default_data_sources(),
        alpha_stack=_default_alpha_stack(),
        notes=notes,
    )
    if alpha_lifecycle.get("items"):
        lifecycle_map = {
            str(item.get("family")): str(item.get("state"))
            for item in list(alpha_lifecycle.get("items", []) or [])
            if str(item.get("family") or "").strip()
        }
        snapshot.alpha_stack = [
            {
                **item,
                "status": lifecycle_map.get(str(item.get("key")), str(item.get("status", ""))),
            }
            for item in snapshot.alpha_stack
        ]
    if not notes:
        snapshot.notes = [
            "Control plane snapshot is running from static artifacts only.",
            "Planned feeds are declared but not fully wired into canonical truth yet.",
        ]
    return asdict(snapshot)


def write_control_plane_snapshot(
    repo_root: Path,
    *,
    output_path: Path | None = None,
    runtime_context_path: Path | None = None,
    site_state_path: Path | None = None,
) -> Path:
    payload = build_control_plane_snapshot(
        repo_root,
        runtime_context_path=runtime_context_path,
        site_state_path=site_state_path,
    )
    path = Path(output_path).resolve() if output_path is not None else (repo_root / "site_portal" / "control_plane_snapshot.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
