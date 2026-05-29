from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..config_utils import ensure_dir
from .config import load_oms_config


def build_oms_paths(config: Dict[str, Any]) -> Dict[str, Path]:
    oms_cfg = load_oms_config(config)
    root = ensure_dir(Path(str(oms_cfg["output_root"])).resolve())
    ledgers = ensure_dir(root / "ledgers")
    snapshots = ensure_dir(root / "snapshots")
    feedback = ensure_dir(root / "feedback")
    history = ensure_dir(root / "history")
    validation = ensure_dir(root / "validation")
    return {
        "root": root,
        "ledgers": ledgers,
        "snapshots": snapshots,
        "feedback": feedback,
        "history": history,
        "validation": validation,
        "account_ledger_latest": ledgers / "account_ledger_latest.csv",
        "position_ledger_latest": ledgers / "position_ledger_latest.csv",
        "intent_ledger_latest": ledgers / "intent_ledger_latest.csv",
        "order_ledger_latest": ledgers / "order_ledger_latest.csv",
        "fill_ledger_latest": ledgers / "fill_ledger_latest.csv",
        "latest_actual_portfolio_state": snapshots / "latest_actual_portfolio_state.json",
        "desired_vs_actual_gap": snapshots / "desired_vs_actual_gap.csv",
        "oms_summary": snapshots / "oms_summary.json",
        "actual_state_daily": snapshots / "actual_state_daily.csv",
        "latest_open_intents": snapshots / "latest_open_intents.json",
        "latest_intent_continuity_report": snapshots / "latest_intent_continuity_report.json",
        "session_resume_audit": snapshots / "session_resume_audit.json",
        "cancel_replace_audit": snapshots / "cancel_replace_audit.json",
        "latest_manual_intervention_state": snapshots / "latest_manual_intervention_state.json",
        "truth_feedback_latest": feedback / "truth_feedback_latest.json",
        "control_feedback_latest": feedback / "control_feedback_latest.json",
        "research_meta_feedback_latest": feedback / "research_meta_feedback_latest.json",
        "narrative_feedback_latest": feedback / "narrative_feedback_latest.json",
        "gap_control_metrics_daily": feedback / "gap_control_metrics_daily.csv",
        "mechanism_realism_rollup": feedback / "mechanism_realism_rollup.csv",
        "manual_overrides": root / "manual_overrides.json",
        "manual_override_history": history / "manual_override_history.jsonl",
        "oms_validation_report": validation / "oms_validation_report.json",
        "oms_validation_summary": validation / "oms_validation_summary.md",
    }
