from __future__ import annotations

from typing import Any, Dict, Protocol

import pandas as pd

from .records import PolicyTuning


class MechanismPolicy(Protocol):
    name: str
    tuning: PolicyTuning
    config: Dict[str, Any]

    def build_profile(self, stock_profile_df: pd.DataFrame, mechanism_map_df: pd.DataFrame) -> pd.DataFrame:
        ...

    def build_source_context(self, source_state_df: pd.DataFrame, as_of_date: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        ...

    def build_state(self, raw_inputs: Dict[str, Any], source_state: pd.DataFrame, context: Dict[str, Any]) -> pd.DataFrame:
        ...

    def map_event_to_stocks(self, event_row: Dict[str, Any], stock_profile_df: pd.DataFrame, context: Dict[str, Any]) -> list[Dict[str, Any]]:
        ...

    def build_core_variables(self, state_df: pd.DataFrame, profile_df: pd.DataFrame, event_rows: pd.DataFrame | None = None, context: Dict[str, Any] | None = None) -> pd.DataFrame:
        ...

    def generate_signal(self, core_variables: pd.DataFrame, base_inputs: Dict[str, Any], context: Dict[str, Any] | None = None) -> pd.DataFrame:
        ...

    def risk_filter(self, signal_row: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        ...

    def entry_rule(self, signal_row: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        ...

    def hold_rule(self, position_state: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        ...

    def exit_rule(self, position_state: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        ...

    def attribution_bucket(self, signal_or_position: Dict[str, Any]) -> str:
        ...

    def attribution_label(self, signal_or_position: Dict[str, Any]) -> str:
        ...
