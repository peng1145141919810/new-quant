from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from .config_utils import ensure_dir
from .sql_store import load_runtime_jsonl_prefer_sql

QUALITY_RANK = {
    "unknown": 0,
    "no_live_snapshot": 0,
    "snapshot_degraded": 1,
    "snapshot_ok": 2,
}

READY_TIMING_STATES = {"buy_ready", "sell_ready", "dual_ready"}
WATCH_TIMING_STATES = {"buy_watch", "sell_watch", "observe"}
FREEZE_REASONS = {
    "oms_not_clean",
    "flow_confirmation_missing",
    "snapshot_unavailable",
    "snapshot_degraded",
    "panic_frozen",
    "major_event_frozen",
    "window_not_allowed",
    "base_position_insufficient",
    "lifecycle_not_allowed",
    "mechanism_t_disabled",
    "event_t_disabled",
    "quality_below_minimum",
}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _normalize_symbol(value: Any) -> str:
    text = _safe_text(value).upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.isdigit():
        suffix = "SH" if text.startswith(("5", "6", "9")) else "SZ"
        return f"{text.zfill(6)}.{suffix}"
    return text


def _load_json(path: Path, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if config is not None:
        from .sql_store import load_runtime_json_prefer_sql

        return load_runtime_json_prefer_sql(config, path, default={})
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_csv(path: Path, config: Dict[str, Any] | None = None) -> pd.DataFrame:
    if config is not None:
        from .sql_store import load_runtime_dataframe_prefer_sql

        return load_runtime_dataframe_prefer_sql(config, path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_csv(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _t_audit_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict(config.get("t_audit", {}) or {})


def _t_audit_root(config: Dict[str, Any]) -> Path:
    default_root = (
        Path(str(config.get("paths", {}).get("data_root", _repo_root() / "data") or _repo_root() / "data")).resolve()
        / "audit_v1"
    )
    root = Path(str(_t_audit_cfg(config).get("artifact_root", default_root) or default_root)).resolve()
    return ensure_dir(root)


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    return ensure_dir(Path(str(config.get("paths", {}).get("trade_clock_root", "") or "")).resolve())


def _intraday_manifest_path(config: Dict[str, Any], trade_date: str) -> Path:
    root = _trade_clock_root(config) / "intraday_state"
    dated = root / str(trade_date or "").replace("-", "") / "intraday_state_manifest.json"
    if dated.exists():
        return dated
    return root / "latest" / "intraday_state_manifest.json"


def _load_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _t_audit_cfg(config)
    policy_path = Path(str(cfg.get("policy_path", "") or "")).resolve()
    payload = _load_json(policy_path, config)
    return payload if isinstance(payload, dict) else {}


def _window_name(value: Any) -> str:
    text = _safe_text(value).lower()
    if text.endswith("_window"):
        return text[:-7]
    return text


def _merge_policy(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in dict(update or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_policy(dict(merged.get(key) or {}), value)
        else:
            merged[key] = value
    return merged


def _quality_rank(label: Any) -> int:
    return QUALITY_RANK.get(_safe_text(label).lower(), 0)


def resolve_t_execution_policy(config: Dict[str, Any], row: Dict[str, Any], timing_window: str = "") -> Dict[str, Any]:
    policy = _load_policy(config)
    defaults = dict(policy.get("defaults", {}) or {})
    mechanism = _safe_text(row.get("mechanism_primary") or "unknown").lower() or "unknown"
    event_type = _safe_text(row.get("primary_event_type") or "unknown").lower() or "unknown"
    lifecycle = _safe_text(row.get("source_lifecycle_state") or row.get("lifecycle_state") or "unknown").lower() or "unknown"
    quality = _safe_text(row.get("feature_quality_tier") or "unknown").lower() or "unknown"
    window_name = _window_name(timing_window or row.get("timing_window") or "")

    merged = dict(defaults)
    merged = _merge_policy(merged, dict(policy.get("mechanism_policies", {}).get(mechanism, {}) or {}))
    merged = _merge_policy(merged, dict(policy.get("event_policies", {}).get(event_type, {}) or {}))
    merged = _merge_policy(merged, dict(policy.get("lifecycle_policies", {}).get(lifecycle, {}) or {}))
    merged = _merge_policy(merged, dict(policy.get("window_policies", {}).get(window_name, {}) or {}))
    merged = _merge_policy(merged, dict(policy.get("quality_policies", {}).get(quality, {}) or {}))

    min_quality = _safe_text(merged.get("min_feature_quality") or defaults.get("min_feature_quality") or "snapshot_ok").lower()
    quality_ok = _quality_rank(quality) >= _quality_rank(min_quality)
    allow_new_t = bool(merged.get("allow_new_t", True)) and quality_ok
    max_t_ratio = _safe_float(merged.get("max_t_ratio", defaults.get("max_t_ratio", 0.0)), 0.0)
    max_t_ratio *= _safe_float(merged.get("quality_ratio_multiplier", 1.0), 1.0)

    reject_reasons: List[str] = []
    if not bool(merged.get("t_allowed", True)):
        reject_reasons.append("mechanism_t_disabled")
    if window_name and window_name in set(merged.get("blocked_windows", []) or []):
        reject_reasons.append("window_not_allowed")
    if lifecycle and lifecycle in set(merged.get("blocked_lifecycle_states", []) or []):
        reject_reasons.append("lifecycle_not_allowed")
    if event_type and event_type in set(merged.get("blocked_event_types", []) or []):
        reject_reasons.append("event_t_disabled")
    if not allow_new_t:
        if quality in {"snapshot_degraded", "no_live_snapshot", "unknown"}:
            reject_reasons.append("snapshot_degraded")
        if not quality_ok:
            reject_reasons.append("quality_below_minimum")
    if max_t_ratio <= 0:
        reject_reasons.append("base_position_insufficient")

    return {
        "mechanism_primary": mechanism,
        "primary_event_type": event_type,
        "lifecycle_state": lifecycle,
        "feature_quality_tier": quality,
        "timing_window": window_name,
        "t_allowed": bool(allow_new_t and not reject_reasons),
        "allow_new_t": bool(allow_new_t),
        "allow_second_leg": bool(merged.get("allow_second_leg", True)),
        "max_t_ratio": round(max(max_t_ratio, 0.0), 6),
        "min_feature_quality": min_quality,
        "blocked_windows": list(merged.get("blocked_windows", []) or []),
        "blocked_lifecycle_states": list(merged.get("blocked_lifecycle_states", []) or []),
        "blocked_event_types": list(merged.get("blocked_event_types", []) or []),
        "reject_reasons": list(dict.fromkeys(reject_reasons)),
    }


def _load_intraday_artifacts(config: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    manifest = _load_json(_intraday_manifest_path(config, trade_date), config)
    intraday_root = _trade_clock_root(config) / "intraday_state" / "latest"
    if manifest:
        artifacts = dict(manifest.get("artifacts", {}) or {})
        if artifacts:
            intraday_root = Path(str(artifacts.get("root", intraday_root) or intraday_root))
    symbol_state = _read_csv(intraday_root / "symbol_execution_state.csv", config)
    intent_state = _read_csv(intraday_root / "intent_state_daily.csv", config)
    control_summary = _load_json(intraday_root / "intraday_control_summary.json", config)
    phase_state = _load_json(intraday_root / "intraday_phase_state.json", config)
    event_log_path = intraday_root / "intraday_event_log.jsonl"
    events = load_runtime_jsonl_prefer_sql(config, event_log_path)
    return {
        "manifest": manifest,
        "root": intraday_root,
        "symbol_state": symbol_state,
        "intent_state": intent_state,
        "control_summary": control_summary,
        "phase_state": phase_state,
        "event_log": events,
    }


def _load_reference_frames(config: Dict[str, Any], release_doc: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    artifacts = dict(release_doc.get("artifacts", {}) or {})
    target_path = Path(_safe_text(artifacts.get("target_positions_path")))
    thesis_root = Path(str(config.get("integrated_thesis", {}).get("output_root", "") or ""))
    thesis_csv = thesis_root / "latest_integrated_thesis.csv"
    return {
        "target_positions": _read_csv(target_path) if target_path.exists() else pd.DataFrame(),
        "integrated_thesis": _read_csv(thesis_csv) if thesis_csv.exists() else pd.DataFrame(),
    }


def _symbol_meta_map(frames: Dict[str, pd.DataFrame]) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for frame_name in ("target_positions", "integrated_thesis"):
        frame = frames.get(frame_name)
        if frame is None or frame.empty:
            continue
        current = frame.copy()
        if "ts_code" in current.columns:
            current["symbol"] = current["ts_code"].map(_normalize_symbol)
        elif "symbol" in current.columns:
            current["symbol"] = current["symbol"].map(_normalize_symbol)
        elif "stock_code" in current.columns:
            current["symbol"] = current["stock_code"].map(_normalize_symbol)
        else:
            continue
        current = current.loc[current["symbol"].astype(str).ne("")]
        for _, row in current.iterrows():
            symbol = _normalize_symbol(row.get("symbol"))
            if not symbol:
                continue
            entry = meta.setdefault(symbol, {})
            payload = dict(row)
            for key in [
                "mechanism_primary",
                "primary_event_type",
                "earnings_reason",
                "source_lifecycle_state",
                "feature_quality_tier",
                "industry",
                "thesis_gate_stage",
                "thesis_reject_reason",
            ]:
                value = payload.get(key)
                if _safe_text(value):
                    entry[key] = value
            if "final_target_weight_v2a" in payload and "target_weight" not in entry:
                entry["target_weight"] = payload.get("final_target_weight_v2a")
            elif "portfolio_weight" in payload and "target_weight" not in entry:
                entry["target_weight"] = payload.get("portfolio_weight")
            elif "target_weight" in payload and "target_weight" not in entry:
                entry["target_weight"] = payload.get("target_weight")
    return meta


def _intent_maps(intent_state: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    if intent_state.empty or "stock_code" not in intent_state.columns:
        return {}
    frame = intent_state.copy()
    frame["symbol"] = frame["stock_code"].map(_normalize_symbol)
    frame = frame.drop_duplicates(subset=["symbol"], keep="last")
    return {
        _normalize_symbol(row.get("symbol")): dict(row)
        for _, row in frame.iterrows()
        if _normalize_symbol(row.get("symbol"))
    }


def _event_flags(event_rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for item in event_rows:
        row = dict(item or {})
        symbol = _normalize_symbol(row.get("stock_code"))
        event_type = _safe_text(row.get("event_type") or row.get("event")).lower()
        if not symbol:
            continue
        entry = per_symbol.setdefault(symbol, {"event_types": [], "manual_override_applied": False})
        if event_type:
            entry["event_types"].append(event_type)
        if event_type == "manual_override_applied":
            entry["manual_override_applied"] = True
    for entry in per_symbol.values():
        entry["event_types"] = list(dict.fromkeys(entry.get("event_types", [])))
    return per_symbol


def _derive_reject_reason(row: Dict[str, Any]) -> str:
    if bool(row.get("t_triggered")):
        return ""
    freeze_reason = _safe_text(row.get("timing_freeze_reason") or row.get("freeze_reason")).strip(";")
    if freeze_reason:
        first = freeze_reason.split(";")[0].strip()
        if first:
            return first
    if not bool(row.get("t_eligible")):
        policy_reasons = list(row.get("policy_reject_reasons", []) or [])
        if policy_reasons:
            return _safe_text(policy_reasons[0])
        return "not_t_eligible"
    if _safe_text(row.get("timing_state")) in WATCH_TIMING_STATES:
        return "watch_only_not_executed"
    if _safe_text(row.get("timing_state")) == "timing_frozen":
        return "timing_frozen"
    return "not_triggered"


def _derive_outcome_label(row: Dict[str, Any]) -> str:
    triggered = bool(row.get("t_triggered"))
    fill_ratio = _safe_float(row.get("fill_ratio"), 0.0)
    before_gap = abs(_safe_float(row.get("before_gap"), 0.0))
    after_gap = abs(_safe_float(row.get("after_gap"), before_gap))
    reject_reason = _safe_text(row.get("reject_reason")).lower()
    quality = _safe_text(row.get("feature_quality_tier")).lower()

    if triggered and fill_ratio > 0:
        if after_gap + 1e-9 < before_gap:
            return "good_execution_improvement"
        return "neutral"
    if triggered and fill_ratio <= 0:
        return "caused_extra_friction"
    if reject_reason in {"snapshot_degraded", "quality_below_minimum", "window_not_allowed", "event_t_disabled", "mechanism_t_disabled"}:
        return "blocked_correctly"
    if reject_reason in {"watch_only_not_executed", "not_triggered"} and quality == "snapshot_ok":
        return "blocked_too_strict"
    if reject_reason in {"panic_blocks_t", "major_event_veto", "safety_halt", "symbol_reconcile_only"}:
        return "should_have_been_frozen"
    return "neutral"


def _rollup(frame: pd.DataFrame, key: str, value_col: str = "symbol_count") -> List[Dict[str, Any]]:
    if frame.empty or key not in frame.columns:
        return []
    current = frame.copy()
    if value_col not in current.columns:
        current[value_col] = 1
    bucket = (
        current.groupby(key, dropna=False)
        .agg(
            symbol_count=("symbol", "nunique"),
            trigger_count=("t_triggered", "sum"),
            execution_count=("executed", "sum"),
            avg_fill_ratio=("fill_ratio", "mean"),
            avg_gap_improvement=("gap_improvement_ratio", "mean"),
            reject_count=("reject_reason", lambda x: int(sum(bool(str(v).strip()) for v in x))),
        )
        .reset_index()
        .rename(columns={key: "bucket"})
        .sort_values(["execution_count", "trigger_count", "symbol_count"], ascending=False)
    )
    for col in ["avg_fill_ratio", "avg_gap_improvement"]:
        bucket[col] = pd.to_numeric(bucket[col], errors="coerce").fillna(0.0).round(6)
    return bucket.to_dict(orient="records")


def _top_pattern_rows(frame: pd.DataFrame, *, success: bool) -> List[Dict[str, Any]]:
    if frame.empty:
        return []
    current = frame.copy()
    current["score"] = pd.to_numeric(current["gap_improvement_ratio"], errors="coerce").fillna(0.0)
    ordered = current.sort_values("score", ascending=not success)
    rows = []
    for _, row in ordered.head(8).iterrows():
        rows.append(
            {
                "symbol": _safe_text(row.get("symbol")),
                "mechanism_primary": _safe_text(row.get("mechanism_primary")),
                "primary_event_type": _safe_text(row.get("primary_event_type")),
                "timing_window": _safe_text(row.get("timing_window")),
                "t_overlay_state": _safe_text(row.get("t_overlay_state")),
                "reject_reason": _safe_text(row.get("reject_reason")),
                "gap_improvement_ratio": round(_safe_float(row.get("gap_improvement_ratio")), 6),
                "fill_ratio": round(_safe_float(row.get("fill_ratio")), 6),
            }
        )
    return rows


def _policy_change_suggestions(frame: pd.DataFrame) -> List[str]:
    suggestions: List[str] = []
    if frame.empty:
        return suggestions
    rejects = frame.loc[frame["reject_reason"].astype(str).ne("")]
    if not rejects.empty:
        top_reject = rejects["reject_reason"].astype(str).value_counts().idxmax()
        top_count = int(rejects["reject_reason"].astype(str).value_counts().iloc[0])
        suggestions.append(f"T 主要阻断原因是 {top_reject}，当前样本 {top_count} 条，后续应优先校准对应 gate。")
    too_strict = frame.loc[frame["outcome_label"].astype(str).eq("blocked_too_strict")]
    if not too_strict.empty:
        top_window = _safe_text(too_strict["timing_window"].astype(str).value_counts().idxmax())
        suggestions.append(f"{top_window or '当前窗口'} 存在 T 过严阻断迹象，应复核该窗口的阈值和二腿放行条件。")
    good = frame.loc[frame["outcome_label"].astype(str).eq("good_execution_improvement")]
    if not good.empty and "mechanism_primary" in good.columns:
        top_mechanism = _safe_text(good["mechanism_primary"].astype(str).value_counts().idxmax())
        suggestions.append(f"{top_mechanism or '当前主机制'} 在样本里最适合做 T，可优先沿机制维度细化限额。")
    degraded = frame.loc[frame["feature_quality_tier"].astype(str).isin(["snapshot_degraded", "no_live_snapshot"])]
    if len(degraded.index) >= 3:
        suggestions.append("实时快照质量退化样本偏多，盘中 T 审查应继续对 degraded snapshot 保持收紧。")
    return suggestions[:4]


def build_t_audit_pack(
    config: Dict[str, Any],
    *,
    trade_date: str,
    release_doc: Dict[str, Any],
    pack_dir: Path | None = None,
) -> Dict[str, Any]:
    if not bool(_t_audit_cfg(config).get("enabled", True)):
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": trade_date,
            "release_id": _safe_text(release_doc.get("release_id")),
            "available": False,
            "reason": "t_audit_disabled",
        }
    intraday = _load_intraday_artifacts(config, trade_date)
    symbol_state = intraday["symbol_state"]
    intent_state = intraday["intent_state"]
    control_summary = dict(intraday.get("control_summary", {}) or {})
    phase_state = dict(intraday.get("phase_state", {}) or {})
    event_flags = _event_flags(list(intraday.get("event_log", []) or []))
    refs = _load_reference_frames(config, release_doc)
    meta_map = _symbol_meta_map(refs)
    intent_map = _intent_maps(intent_state)

    if symbol_state.empty:
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": trade_date,
            "release_id": _safe_text(release_doc.get("release_id")),
            "available": False,
            "reason": "missing_symbol_execution_state",
        }
        root = _t_audit_root(config)
        latest_root = ensure_dir(root / "latest")
        archive_root = ensure_dir(root / str(trade_date).replace("-", ""))
        _write_json(latest_root / "latest_t_audit.json", payload)
        _write_json(archive_root / "t_audit.json", payload)
        if pack_dir is not None:
            _write_json(ensure_dir(pack_dir) / "t_audit.json", payload)
        return payload

    frame = symbol_state.copy()
    frame["symbol"] = frame["stock_code"].map(_normalize_symbol)
    frame["trade_date"] = frame.get("trade_date", pd.Series([trade_date] * len(frame.index))).astype(str)
    frame["target_weight"] = pd.to_numeric(frame.get("target_weight", 0.0), errors="coerce").fillna(0.0)
    frame["actual_weight"] = pd.to_numeric(frame.get("actual_weight", 0.0), errors="coerce").fillna(0.0)
    frame["desired_vs_actual_gap"] = pd.to_numeric(frame.get("desired_vs_actual_gap", 0.0), errors="coerce").fillna(0.0)
    rows: List[Dict[str, Any]] = []
    for _, series in frame.iterrows():
        row = dict(series.to_dict())
        symbol = _normalize_symbol(row.get("symbol") or row.get("stock_code"))
        meta = dict(meta_map.get(symbol, {}) or {})
        intent = dict(intent_map.get(symbol, {}) or {})
        flags = dict(event_flags.get(symbol, {}) or {})
        combined = dict(meta)
        combined.update(row)
        policy = resolve_t_execution_policy(config, combined, timing_window=row.get("timing_window"))
        fill_ratio = _safe_float(intent.get("fill_ratio"), 0.0)
        before_gap = abs(_safe_float(row.get("desired_vs_actual_gap"), 0.0))
        executed = bool(_safe_text(row.get("t_leg_done")) in {"sell_leg", "buy_leg", "completed"}) or fill_ratio > 0
        after_gap = max(before_gap * (1.0 - max(min(fill_ratio, 1.0), 0.0)), 0.0) if executed else before_gap
        gap_improvement = before_gap - after_gap
        reject_reason = _derive_reject_reason(
            {
                **row,
                "policy_reject_reasons": policy.get("reject_reasons", []),
                "fill_ratio": fill_ratio,
            }
        )
        audit_row = {
            "trade_date": _safe_text(row.get("trade_date") or trade_date),
            "release_id": _safe_text(row.get("release_id") or release_doc.get("release_id")),
            "symbol": symbol,
            "current_phase": _safe_text(row.get("current_phase") or phase_state.get("current_phase")),
            "timing_window": _safe_text(row.get("timing_window") or control_summary.get("timing_window") or phase_state.get("timing_window")),
            "symbol_state": _safe_text(row.get("symbol_state")),
            "source_lifecycle_state": _safe_text(row.get("source_lifecycle_state") or meta.get("source_lifecycle_state")),
            "feature_quality_tier": _safe_text(row.get("feature_quality_tier") or meta.get("feature_quality_tier") or "unknown"),
            "mechanism_primary": _safe_text(row.get("mechanism_primary") or meta.get("mechanism_primary") or "unlabeled"),
            "primary_event_type": _safe_text(row.get("primary_event_type") or meta.get("primary_event_type") or "unknown"),
            "earnings_reason": _safe_text(row.get("earnings_reason") or meta.get("earnings_reason") or "unknown"),
            "target_weight": round(_safe_float(row.get("target_weight")), 6),
            "actual_weight": round(_safe_float(row.get("actual_weight")), 6),
            "before_gap": round(before_gap, 6),
            "after_gap": round(after_gap, 6),
            "gap_change": round(gap_improvement, 6),
            "gap_improvement_ratio": round(gap_improvement / before_gap, 6) if before_gap > 0 else 0.0,
            "timing_state": _safe_text(row.get("timing_state")),
            "t_overlay_state": _safe_text(row.get("t_overlay_state")),
            "t_direction": _safe_text(row.get("t_direction")),
            "t_eligible": bool(row.get("t_eligible")),
            "t_triggered": bool(row.get("t_triggered")),
            "executed": bool(executed),
            "fill_ratio": round(fill_ratio, 6),
            "last_intent_state": _safe_text(intent.get("intent_state") or row.get("last_intent_state")),
            "last_order_state": _safe_text(intent.get("order_status") or row.get("last_order_state")),
            "timing_freeze_reason": _safe_text(row.get("timing_freeze_reason")),
            "freeze_reason": _safe_text(row.get("freeze_reason")),
            "reject_reason": reject_reason,
            "policy_t_allowed": bool(policy.get("t_allowed")),
            "policy_allow_second_leg": bool(policy.get("allow_second_leg")),
            "policy_max_t_ratio": round(_safe_float(policy.get("max_t_ratio")), 6),
            "policy_reject_reasons": list(policy.get("reject_reasons", []) or []),
            "event_types": list(flags.get("event_types", []) or []),
            "manual_override_applied": bool(flags.get("manual_override_applied", False)),
        }
        audit_row["outcome_label"] = _derive_outcome_label(audit_row)
        rows.append(audit_row)

    audit_frame = pd.DataFrame(rows)
    if audit_frame.empty:
        audit_frame = pd.DataFrame(columns=["trade_date", "release_id", "symbol"])

    window_daily = (
        audit_frame.groupby("timing_window", dropna=False)
        .agg(
            symbol_count=("symbol", "nunique"),
            t_eligible_symbols=("t_eligible", "sum"),
            t_triggered_symbols=("t_triggered", "sum"),
            execution_count=("executed", "sum"),
            avg_fill_ratio=("fill_ratio", "mean"),
            avg_gap_improvement=("gap_improvement_ratio", "mean"),
        )
        .reset_index()
        .sort_values("execution_count", ascending=False)
    )
    reject_summary = (
        audit_frame.loc[audit_frame["reject_reason"].astype(str).ne("")]
        .groupby("reject_reason", dropna=False)
        .agg(symbol_count=("symbol", "nunique"), sample_count=("symbol", "count"))
        .reset_index()
        .sort_values(["sample_count", "symbol_count"], ascending=False)
    )
    mechanism_summary = pd.DataFrame(_rollup(audit_frame, "mechanism_primary"))
    event_summary = pd.DataFrame(_rollup(audit_frame, "primary_event_type"))
    quality_summary = pd.DataFrame(_rollup(audit_frame, "feature_quality_tier"))

    summary_lines = [
        "T 审计衡量的是盘中执行质量改善和阻断原因，不把它伪装成独立 alpha。",
        f"当前样本共 {int(len(audit_frame.index))} 个标的，T 合格 {int(audit_frame['t_eligible'].sum())} 个，触发 {int(audit_frame['t_triggered'].sum())} 个。",
        f"实际进入执行 {int(audit_frame['executed'].sum())} 个，平均成交比例 {_safe_float(audit_frame['fill_ratio'].mean() if not audit_frame.empty else 0.0):.2%}。",
    ]
    top_reject = dict(reject_summary.iloc[0].to_dict()) if not reject_summary.empty else {}
    top_mechanism = dict(mechanism_summary.iloc[0].to_dict()) if not mechanism_summary.empty else {}
    if top_reject:
        summary_lines.append(
            f"最常见阻断原因是 {_safe_text(top_reject.get('reject_reason'))}，样本 {int(_safe_int(top_reject.get('sample_count')))} 条。"
        )
    if top_mechanism:
        summary_lines.append(
            f"当前最适合做 T 的机制是 {_safe_text(top_mechanism.get('bucket'))}，触发 {int(_safe_int(top_mechanism.get('trigger_count')))} 次。"
        )

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "release_id": _safe_text(release_doc.get("release_id")),
        "available": True,
        "mode": "intraday_overlay_sidecar_audit",
        "summary_lines": summary_lines,
        "counts": {
            "symbol_count": int(audit_frame["symbol"].nunique()),
            "t_eligible_symbols": int(audit_frame["t_eligible"].sum()),
            "t_triggered_symbols": int(audit_frame["t_triggered"].sum()),
            "execution_count": int(audit_frame["executed"].sum()),
        },
        "top_reject_reason": _safe_text(top_reject.get("reject_reason")),
        "top_suited_mechanism": _safe_text(top_mechanism.get("bucket")),
        "window_daily": window_daily.to_dict(orient="records"),
        "reject_reason_summary": reject_summary.to_dict(orient="records"),
        "mechanism_summary": mechanism_summary.to_dict(orient="records"),
        "event_summary": event_summary.to_dict(orient="records"),
        "quality_summary": quality_summary.to_dict(orient="records"),
        "top_success_cases": _top_pattern_rows(audit_frame.loc[audit_frame["outcome_label"].eq("good_execution_improvement")], success=True),
        "top_problem_cases": _top_pattern_rows(audit_frame.loc[audit_frame["outcome_label"].isin(["blocked_too_strict", "caused_extra_friction"])], success=False),
        "policy_change_suggestions": _policy_change_suggestions(audit_frame),
        "source_paths": {
            "intraday_root": str(intraday.get("root")),
            "intraday_manifest": str(_intraday_manifest_path(config, trade_date)),
        },
    }

    root = _t_audit_root(config)
    latest_root = ensure_dir(root / "latest")
    archive_root = ensure_dir(root / str(trade_date).replace("-", ""))
    _write_json(latest_root / "latest_t_audit.json", payload)
    _write_json(archive_root / "t_audit.json", payload)
    _write_csv(latest_root / "t_overlay_window_daily.csv", window_daily)
    _write_csv(latest_root / "t_overlay_reject_reasons.csv", reject_summary)
    _write_csv(latest_root / "t_overlay_mechanism_summary.csv", mechanism_summary)
    _write_csv(latest_root / "t_overlay_event_summary.csv", event_summary)
    _write_csv(latest_root / "t_overlay_quality_summary.csv", quality_summary)
    _write_csv(archive_root / "t_overlay_window_daily.csv", window_daily)
    _write_csv(archive_root / "t_overlay_reject_reasons.csv", reject_summary)
    _write_csv(archive_root / "t_overlay_mechanism_summary.csv", mechanism_summary)
    _write_csv(archive_root / "t_overlay_event_summary.csv", event_summary)
    _write_csv(archive_root / "t_overlay_quality_summary.csv", quality_summary)

    if pack_dir is not None:
        report_root = ensure_dir(pack_dir)
        _write_json(report_root / "t_audit.json", payload)
        _write_csv(report_root / "t_overlay_window_daily.csv", window_daily)

    return payload
