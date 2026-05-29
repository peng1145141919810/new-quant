from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

import pandas as pd


@dataclass
class BaseMechanismPolicy:
    name: str
    tuning: Any
    config: Dict[str, Any]
    profile_builder: Callable[..., pd.DataFrame]
    source_summarizer: Callable[..., Dict[str, Any]]
    state_builder: Callable[..., pd.DataFrame]
    mapping_builder: Callable[..., List[Dict[str, Any]]]
    core_variable_builder: Callable[..., pd.DataFrame]
    signal_builder: Callable[..., pd.DataFrame]
    risk_builder: Callable[..., Dict[str, Any]]
    entry_rule_fn: Callable[..., bool]
    hold_rule_fn: Callable[..., bool]
    exit_rule_fn: Callable[..., bool]
    attribution_bucket_fn: Callable[..., str]
    attribution_label_fn: Callable[..., str]
    extra_context: Dict[str, Any] = field(default_factory=dict)

    def build_profile(self, stock_profile_df: pd.DataFrame, mechanism_map_df: pd.DataFrame) -> pd.DataFrame:
        return self.profile_builder(stock_profile_df=stock_profile_df, mechanism_map_df=mechanism_map_df, tuning=self.tuning, config=self.config)

    def build_source_context(self, source_state_df: pd.DataFrame, as_of_date: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return self.source_summarizer(source_state_df=source_state_df, as_of_date=as_of_date, tuning=self.tuning, config=self.config, context=context or {})

    def build_state(self, raw_inputs: Dict[str, Any], source_state: pd.DataFrame, context: Dict[str, Any]) -> pd.DataFrame:
        return self.state_builder(raw_inputs=raw_inputs, source_state=source_state, context={**self.extra_context, **context}, tuning=self.tuning, config=self.config)

    def map_event_to_stocks(self, event_row: Dict[str, Any], stock_profile_df: pd.DataFrame, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.mapping_builder(event_row=event_row, stock_profile_df=stock_profile_df, context={**self.extra_context, **context}, tuning=self.tuning, config=self.config)

    def build_core_variables(
        self,
        state_df: pd.DataFrame,
        profile_df: pd.DataFrame,
        event_rows: pd.DataFrame | None = None,
        context: Dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        return self.core_variable_builder(
            state_df=state_df,
            profile_df=profile_df,
            event_rows=event_rows if event_rows is not None else pd.DataFrame(),
            context={**self.extra_context, **(context or {})},
            tuning=self.tuning,
            config=self.config,
        )

    def risk_filter(self, signal_row: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return self.risk_builder(signal_row=signal_row, context={**self.extra_context, **(context or {})}, tuning=self.tuning, config=self.config)

    def generate_signal(self, core_variables: pd.DataFrame, base_inputs: Dict[str, Any], context: Dict[str, Any] | None = None) -> pd.DataFrame:
        return self.signal_builder(
            core_variables=core_variables,
            base_inputs=base_inputs,
            context={**self.extra_context, **(context or {})},
            tuning=self.tuning,
            config=self.config,
            risk_filter=self.risk_filter,
            attribution_bucket=self.attribution_bucket,
            attribution_label=self.attribution_label,
        )

    def entry_rule(self, signal_row: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        return bool(self.entry_rule_fn(signal_row=signal_row, context={**self.extra_context, **(context or {})}, tuning=self.tuning, config=self.config))

    def hold_rule(self, position_state: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        return bool(self.hold_rule_fn(position_state=position_state, context={**self.extra_context, **(context or {})}, tuning=self.tuning, config=self.config))

    def exit_rule(self, position_state: Dict[str, Any], context: Dict[str, Any] | None = None) -> bool:
        return bool(self.exit_rule_fn(position_state=position_state, context={**self.extra_context, **(context or {})}, tuning=self.tuning, config=self.config))

    def attribution_bucket(self, signal_or_position: Dict[str, Any]) -> str:
        return str(self.attribution_bucket_fn(signal_or_position=signal_or_position, tuning=self.tuning, config=self.config) or 'state')

    def attribution_label(self, signal_or_position: Dict[str, Any]) -> str:
        return str(self.attribution_label_fn(signal_or_position=signal_or_position, tuning=self.tuning, config=self.config) or self.name)
