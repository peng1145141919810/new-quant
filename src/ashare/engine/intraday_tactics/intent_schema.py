from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal

Side = Literal["BUY", "SELL"]

INTENT_CLASS_PRIORITY: Dict[str, int] = {
    "manual_override": 1,
    "risk_exit": 2,
    "event_veto_exit": 3,
    "stop_loss": 4,
    "liquidity_reduce": 5,
    "concentration_reduce": 6,
    "time_stop": 7,
    "take_profit": 8,
    "signal_decay_reduce": 9,
    "t_overlay": 10,
    "tactical_add": 11,
}


@dataclass
class IntradayActionIntent:
    intent_id: str
    trade_date: str
    symbol: str
    side: str
    intent_class: str
    reason_code: str
    rule_id: str
    priority: int
    delta_shares: int
    delta_notional_cap: float
    full_exit_flag: bool
    reduce_only: bool
    allow_conflict_with_release: bool
    source_window: str
    timing_state: str
    t_overlay_state: str
    feature_quality_tier: str
    lifecycle_state: str
    mechanism_primary: str
    primary_event_type: str
    cooldown_key: str
    cooldown_until: str
    max_rounds_today: int
    valid_from: str
    valid_until: str
    created_at: str
    created_by_module: str
    debug_payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IntradayIntentConflict:
    symbol: str
    winner_intent_id: str
    suppressed_intent_ids: List[str]
    resolution: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IntradayTacticalRunSummary:
    ok: bool
    trade_date: str
    tactical_phase: str
    n_raw_intents: int
    n_arbitrated: int
    n_orders: int
    n_blocked: int
    artifact_paths: Dict[str, str] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
