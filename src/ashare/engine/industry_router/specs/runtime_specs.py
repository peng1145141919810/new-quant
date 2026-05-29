from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..core.common import safe_float, safe_text
from ..schemas import EvidenceTypeSpec, IndustryStrategySpec, ShockTypeSpec


def load_strategy_spec(contract_root_path: Path) -> IndustryStrategySpec:
    path = contract_root_path / "strategy_spec.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    shock_types = {
        name: ShockTypeSpec(
            shock_type=name,
            half_life_days=int(dict(item).get("half_life_days", 20) or 20),
            min_persistence_days=int(dict(item).get("min_persistence_days", 10) or 10),
            invalidate_conditions=list(dict(item).get("invalidate_conditions", []) or []),
        )
        for name, item in dict(payload.get("shock_types", {}) or {}).items()
    }
    evidence_types = {
        name: EvidenceTypeSpec(
            evidence_type=name,
            base_weight=safe_float(dict(item).get("base_weight"), 0.1),
            max_share=safe_float(dict(item).get("max_share"), 0.2),
        )
        for name, item in dict(payload.get("evidence_types", {}) or {}).items()
    }
    return IndustryStrategySpec(
        spec_version=safe_text(payload.get("spec_version")) or "industry_router_thesis_v1",
        thesis_policy=dict(payload.get("thesis_policy", {}) or {}),
        stock_signal_policy=dict(payload.get("stock_signal_policy", {}) or {}),
        scoring_weights={key: safe_float(value, 0.0) for key, value in dict(payload.get("scoring_weights", {}) or {}).items()},
        shock_types=shock_types,
        evidence_types=evidence_types,
    )


def evidence_type_weight(strategy_spec: IndustryStrategySpec, evidence_type: str) -> float:
    item = strategy_spec.evidence_types.get(safe_text(evidence_type))
    return float(item.base_weight if item else 0.1)


def resolve_source_evidence_type(mechanism_primary: str, source_name: str, title: str, summary: str, category: str) -> str:
    text = " ".join([safe_text(source_name), safe_text(title), safe_text(summary), safe_text(category)]).lower()
    mechanism = safe_text(mechanism_primary)
    if mechanism == "price_inventory":
        if any(token in text for token in ["库存", "仓单", "累库", "去库", "inventory"]):
            return "inventory"
        if any(token in text for token in ["价格", "ppi", "价差", "price"]):
            return "price"
        return "trade"
    if mechanism == "macro_style":
        if any(token in text for token in ["社融", "m2", "利率", "政策", "流动性", "pmi"]):
            return "policy"
        return "trade"
    if any(token in text for token in ["出口", "贸易", "海关"]):
        return "trade"
    return "production"
