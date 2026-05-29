from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class ThemeSpec:
    theme_id: str
    theme_name: str
    theme_type: str
    description: str
    primary_chain: str
    primary_data_sources: List[str]
    update_frequency: str
    active_flag: bool
    mechanism_primary: str
    default_shock_type: str
    key_terms: List[str]


@dataclass(frozen=True)
class CompanyExposure:
    ts_code: str
    theme_id: str
    theme_name: str
    mechanism_primary: str
    chain_position: str
    exposure_strength: float
    benefit_direction: str
    purity_score: float
    profit_path: str
    evidence_note: str
    mapping_confidence: float
    active_flag: bool
    symbol: str = ""
    code: str = ""
    name: str = ""
    industry_primary: str = ""
    subchain_primary: str = ""


@dataclass(frozen=True)
class ShockTypeSpec:
    shock_type: str
    half_life_days: int
    min_persistence_days: int
    invalidate_conditions: List[str]


@dataclass(frozen=True)
class EvidenceTypeSpec:
    evidence_type: str
    base_weight: float
    max_share: float


@dataclass(frozen=True)
class IndustryStrategySpec:
    spec_version: str
    thesis_policy: Dict[str, Any]
    stock_signal_policy: Dict[str, Any]
    scoring_weights: Dict[str, float]
    shock_types: Dict[str, ShockTypeSpec]
    evidence_types: Dict[str, EvidenceTypeSpec]


@dataclass(frozen=True)
class EvidenceItem:
    theme_id: str
    evidence_type: str
    evidence_name: str
    value: float
    source: str
    date: str
    quality_flag: str
    direction: str
    notes: str
    confidence: float = 0.0
    weight: float = 0.0
    source_id: str = ""
    mechanism_primary: str = ""
    related_symbol: str = ""

    def signed_value(self) -> float:
        if self.direction == "positive":
            return float(self.value)
        if self.direction == "negative":
            return -abs(float(self.value))
        return float(self.value) * 0.15


@dataclass
class EvidenceBundle:
    theme_id: str
    items: List[EvidenceItem] = field(default_factory=list)

    @property
    def evidence_count(self) -> int:
        return len(self.items)

    @property
    def non_event_count(self) -> int:
        return sum(1 for item in self.items if item.evidence_type != "event_clue")

    @property
    def event_count(self) -> int:
        return sum(1 for item in self.items if item.evidence_type == "event_clue")

    @property
    def evidence_types(self) -> List[str]:
        seen: List[str] = []
        for item in self.items:
            if item.evidence_type not in seen:
                seen.append(item.evidence_type)
        return seen

    def to_rows(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.items]


@dataclass(frozen=True)
class ThesisScoreCard:
    evidence_score: float
    causal_clarity_score: float
    persistence_score: float
    exposure_score: float
    underpricing_score: float
    crowding_penalty: float
    final_score: float


@dataclass(frozen=True)
class IndustryThesis:
    thesis_id: str
    theme_id: str
    theme_name: str
    mechanism_primary: str
    shock_type: str
    title: str
    start_date: str
    state: str
    evidence_bundle: EvidenceBundle
    persistence_assessment: str
    crowding_assessment: str
    affected_chain_nodes: List[str]
    beneficiary_symbols: List[str]
    invalidate_conditions: List[str]
    score_card: ThesisScoreCard
    allow_entry: bool
    audit: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["evidence_bundle"] = {
            "theme_id": self.evidence_bundle.theme_id,
            "evidence_count": self.evidence_bundle.evidence_count,
            "non_event_count": self.evidence_bundle.non_event_count,
            "event_count": self.evidence_bundle.event_count,
            "evidence_types": self.evidence_bundle.evidence_types,
            "items": self.evidence_bundle.to_rows(),
        }
        return payload


@dataclass(frozen=True)
class StockSignal:
    symbol: str
    ts_code: str
    code: str
    name: str
    trade_date: str
    date: str
    theme_id: str
    theme_name: str
    thesis_id: str
    shock_type: str
    mechanism_primary: str
    industry_primary: str
    subchain_primary: str
    chain_position: str
    benefit_direction: str
    profit_path: str
    evidence_score: float
    causal_clarity_score: float
    persistence_score: float
    exposure_score: float
    underpricing_score: float
    crowding_penalty: float
    final_score: float
    allow_entry: bool
    signal_state: str
    thesis_state: str
    thesis_support_score: float
    stock_specific_event_score: float
    mapping_confidence: float
    latest_close: float
    price_date: str
    price_source: str
    event_clue_count: int
    non_event_evidence_count: int
    evidence_types: str
    reason_top: str
    reason_detail_json: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
