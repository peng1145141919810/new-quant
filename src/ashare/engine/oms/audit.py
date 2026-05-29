from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pandas as pd

from .contracts.intent_schema import OPEN_INTENT_STATUSES


CONTROL_DAILY_FIELDS = [
    "generated_at",
    "release_id",
    "recent_new_entry_completion_ratio",
    "recent_add_completion_ratio",
    "recent_trim_completion_ratio",
    "recent_exit_completion_ratio",
    "turnover_truncation_ratio",
    "persistent_gap_ratio",
    "stuck_open_intent_count",
    "median_time_to_completion_hours",
    "partial_stuck_symbol_ratio",
    "release_convergence_score",
    "replacement_churn_score",
]

MECHANISM_ROLLUP_FIELDS = [
    "generated_at",
    "window_runs",
    "mechanism_primary",
    "desired_count",
    "realized_count",
    "realization_ratio",
    "gap_pressure",
    "convergence_score",
    "non_executable_ratio",
]


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _recent_release_subset(frame: pd.DataFrame, lookback_runs: int) -> pd.DataFrame:
    if frame is None or frame.empty or "release_id" not in frame.columns:
        return pd.DataFrame(columns=getattr(frame, "columns", []))
    bucket = frame.copy()
    time_col = "updated_at" if "updated_at" in bucket.columns else "date"
    bucket["sort_time"] = pd.to_datetime(bucket.get(time_col, ""), errors="coerce")
    release_order = (
        bucket.groupby(bucket["release_id"].astype(str))["sort_time"]
        .max()
        .sort_values()
        .index.astype(str)
        .tolist()
    )
    keep = set(release_order[-max(int(lookback_runs or 1), 1):])
    out = bucket.loc[bucket["release_id"].astype(str).isin(keep)].copy()
    if "sort_time" in out.columns:
        out = out.drop(columns=["sort_time"])
    return out


def _recent_dates(frame: pd.DataFrame, limit: int) -> List[str]:
    if frame is None or frame.empty or "date" not in frame.columns:
        return []
    bucket = frame.copy()
    bucket["date_only"] = bucket["date"].astype(str).str.slice(0, 10)
    dates = sorted({item for item in bucket["date_only"].tolist() if str(item).strip()})
    return dates[-max(int(limit or 1), 1):]


def _terminal_completion_hours(intent_frame: pd.DataFrame) -> float:
    if intent_frame is None or intent_frame.empty:
        return 0.0
    terminal = intent_frame.loc[~intent_frame["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy()
    if terminal.empty:
        return 0.0
    created = pd.to_datetime(terminal.get("created_at", ""), errors="coerce")
    updated = pd.to_datetime(terminal.get("updated_at", ""), errors="coerce")
    hours = (updated - created).dt.total_seconds() / 3600.0
    hours = hours.replace([float("inf"), float("-inf")], pd.NA).dropna()
    if hours.empty:
        return 0.0
    return round(float(hours.median()), 6)


def build_feedback_buckets(
    oms_summary: Dict[str, Any],
    actual_state_frame: pd.DataFrame,
    intent_frame: pd.DataFrame,
    order_frame: pd.DataFrame,
    fill_frame: pd.DataFrame,
    actual_state_history_frame: pd.DataFrame | None = None,
    control_lookback_runs: int = 20,
    research_lookback_runs: int = 60,
) -> Dict[str, Any]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    intent_frame = intent_frame.copy() if intent_frame is not None else pd.DataFrame()
    order_frame = order_frame.copy() if order_frame is not None else pd.DataFrame()
    fill_frame = fill_frame.copy() if fill_frame is not None else pd.DataFrame()
    actual_state_frame = actual_state_frame.copy() if actual_state_frame is not None else pd.DataFrame()
    history_frame = actual_state_history_frame.copy() if actual_state_history_frame is not None else actual_state_frame.copy()

    if "action_type" not in intent_frame.columns:
        intent_frame["action_type"] = ""
    if "status" not in intent_frame.columns:
        intent_frame["status"] = ""
    if "mechanism_primary" not in actual_state_frame.columns:
        actual_state_frame["mechanism_primary"] = ""
    if "actual_state" not in actual_state_frame.columns:
        actual_state_frame["actual_state"] = ""
    if "mechanism_primary" not in history_frame.columns:
        history_frame["mechanism_primary"] = ""
    if "actual_state" not in history_frame.columns:
        history_frame["actual_state"] = ""

    recent_intents = _recent_release_subset(intent_frame, lookback_runs=control_lookback_runs)

    def _completion_ratio(action_type: str) -> float:
        subset = recent_intents.loc[recent_intents["action_type"].astype(str) == action_type].copy()
        if subset.empty:
            return 0.0
        terminal = subset.loc[~subset["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy()
        if terminal.empty:
            return 0.0
        return _safe_ratio(float((terminal["status"].astype(str) == "filled").sum()), float(len(terminal.index)))

    open_recent = recent_intents.loc[recent_intents["status"].astype(str).isin(OPEN_INTENT_STATUSES)].copy()
    partial_stuck = open_recent.loc[open_recent["status"].astype(str).isin(["acknowledged", "partial_fill", "cancel_requested"])].copy()
    replacement_count = 0
    if not recent_intents.empty:
        replacement_count = int((recent_intents.get("supersedes_intent_id", "").astype(str).str.strip() != "").sum())
        replacement_count += int((recent_intents.get("replaced_by_intent_id", "").astype(str).str.strip() != "").sum())

    control_feedback = {
        "generated_at": now_text,
        "recent_new_entry_completion_ratio": _completion_ratio("new"),
        "recent_add_completion_ratio": _completion_ratio("add"),
        "recent_trim_completion_ratio": _completion_ratio("trim"),
        "recent_exit_completion_ratio": _completion_ratio("exit"),
        "turnover_truncation_ratio": round(float(oms_summary.get("dispatch", {}).get("turnover_truncation_ratio", 0.0) or 0.0), 6),
        "persistent_gap_ratio": round(float(oms_summary.get("gap", {}).get("gap_weight_ratio", 0.0) or 0.0), 6),
        "stuck_open_intent_count": int(len(open_recent.index)),
        "median_time_to_completion_hours": _terminal_completion_hours(recent_intents),
        "partial_stuck_symbol_ratio": _safe_ratio(float(len(partial_stuck.index)), float(max(len(open_recent.index), 1))),
        "release_convergence_score": round(max(0.0, 1.0 - min(float(oms_summary.get("gap", {}).get("gap_weight_ratio", 0.0) or 0.0), 1.0)), 6),
        "replacement_churn_score": _safe_ratio(float(replacement_count), float(max(len(recent_intents.index), 1))),
    }
    control_daily_frame = pd.DataFrame([{key: control_feedback.get(key, pd.NA) for key in CONTROL_DAILY_FIELDS}])

    window_candidates = sorted({20, 40, max(int(research_lookback_runs or 60), 10)})
    mechanism_rows: List[Dict[str, Any]] = []
    for window in window_candidates:
        dates = _recent_dates(history_frame, limit=window)
        if not dates:
            continue
        subset = history_frame.loc[history_frame["date"].astype(str).str.slice(0, 10).isin(dates)].copy()
        if subset.empty:
            continue
        for mechanism, bucket in subset.groupby(subset["mechanism_primary"].astype(str)):
            if not str(mechanism or "").strip():
                continue
            target_weight_sum = float(pd.to_numeric(bucket.get("target_weight", 0.0), errors="coerce").fillna(0.0).abs().sum())
            desired_mask = pd.to_numeric(bucket.get("target_weight", 0.0), errors="coerce").fillna(0.0) > 0
            desired_count = float(max(int(desired_mask.sum()), len(bucket.index)))
            realized_count = float((bucket["actual_state"].astype(str) != "watch").sum())
            gap_pressure = float(pd.to_numeric(bucket.get("gap_weight_abs", 0.0), errors="coerce").fillna(0.0).sum())
            non_executable = float(((bucket["actual_state"].astype(str) == "watch") & desired_mask).sum())
            convergence = 1.0
            if target_weight_sum > 0:
                convergence = max(0.0, 1.0 - min(gap_pressure / target_weight_sum, 1.0))
            mechanism_rows.append(
                {
                    "generated_at": now_text,
                    "window_runs": int(window),
                    "mechanism_primary": str(mechanism),
                    "desired_count": int(desired_count),
                    "realized_count": int(realized_count),
                    "realization_ratio": _safe_ratio(realized_count, desired_count),
                    "gap_pressure": round(gap_pressure, 6),
                    "convergence_score": round(convergence, 6),
                    "non_executable_ratio": _safe_ratio(non_executable, desired_count),
                }
            )
    mechanism_rollup_frame = pd.DataFrame(mechanism_rows)
    if mechanism_rollup_frame.empty:
        mechanism_rollup_frame = pd.DataFrame(columns=MECHANISM_ROLLUP_FIELDS)
    else:
        for col in MECHANISM_ROLLUP_FIELDS:
            if col not in mechanism_rollup_frame.columns:
                mechanism_rollup_frame[col] = pd.NA
        mechanism_rollup_frame = mechanism_rollup_frame[MECHANISM_ROLLUP_FIELDS].copy()

    repeated_non_executable_symbols: List[str] = []
    if not history_frame.empty and "symbol" in history_frame.columns:
        hist = history_frame.copy()
        hist["target_weight_num"] = pd.to_numeric(hist.get("target_weight", 0.0), errors="coerce").fillna(0.0)
        hist = hist.loc[(hist["actual_state"].astype(str) == "watch") & (hist["target_weight_num"] > 0)].copy()
        if not hist.empty:
            repeated_non_executable_symbols = hist["symbol"].astype(str).value_counts().head(12).index.tolist()

    research_meta_feedback = {
        "generated_at": now_text,
        "mechanism_execution_realization": mechanism_rollup_frame.to_dict(orient="records")[:18],
        "repeated_non_executable_symbols": repeated_non_executable_symbols,
        "control_feedback_bridge": {
            "release_convergence_score": control_feedback["release_convergence_score"],
            "replacement_churn_score": control_feedback["replacement_churn_score"],
        },
    }

    narrative_feedback = {
        "generated_at": now_text,
        "summary": (
            f"OMS observed {int(len(intent_frame.index))} intents, {int(len(order_frame.index))} orders, "
            f"and {int(len(fill_frame.index))} fills; current persistent gap ratio="
            f"{control_feedback['persistent_gap_ratio']:.4f}, convergence="
            f"{control_feedback['release_convergence_score']:.4f}."
        ),
        "notes": [
            "Narrative feedback is non-authoritative.",
            "Broker/account truth remains in OMS ledgers and actual-state artifacts.",
        ],
    }

    truth_feedback = {
        "generated_at": now_text,
        "account_truth_available": bool(oms_summary.get("account", {})),
        "order_truth_rows": int(len(order_frame.index)),
        "fill_truth_rows": int(len(fill_frame.index)),
        "actual_state_rows": int(len(actual_state_frame.index)),
    }
    return {
        "truth": truth_feedback,
        "control": control_feedback,
        "research_meta": research_meta_feedback,
        "narrative": narrative_feedback,
        "control_daily": control_daily_frame,
        "mechanism_rollup": mechanism_rollup_frame,
    }
