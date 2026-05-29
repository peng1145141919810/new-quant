from __future__ import annotations

import json
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .alpha_attribution import build_alpha_attribution
from .alpha_lifecycle import summarize_alpha_lifecycle_lines
from .config_utils import ensure_dir
from .oms.paths import build_oms_paths
from .intraday_tactical_audit_pack import build_intraday_tactical_audit_pack
from .t_audit import build_t_audit_pack


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _audit_config_for_release(config: Dict[str, Any], release_doc: Dict[str, Any]) -> Dict[str, Any]:
    release_artifacts = dict(release_doc.get("artifacts", {}) or {})
    candidate_paths = [
        Path(_safe_text(release_artifacts.get("manifest_path"))),
        Path(_safe_text(release_artifacts.get("target_positions_path"))),
        Path(_safe_text(release_artifacts.get("portfolio_summary_path"))),
    ]
    existing = [path for path in candidate_paths if _safe_text(path) and path.exists()]
    if not existing:
        return config
    release_dir = existing[0].parent
    latest_execution_path = release_dir / "latest_execution.json"
    latest_execution = _load_json(latest_execution_path) if latest_execution_path.exists() else {}
    namespace = _safe_text(latest_execution.get("execution_report", {}).get("execution_namespace"))
    if not namespace:
        namespace = _safe_text(latest_execution.get("execution_namespace"))
    if not namespace:
        execution_report_path = Path(_safe_text(latest_execution.get("execution_report_path")))
        execution_report = _load_json(execution_report_path) if execution_report_path.exists() else {}
        namespace = _safe_text(execution_report.get("execution_namespace"))
        if not namespace:
            namespace = _safe_text(execution_report.get("execution_policy", {}).get("namespace"))
    if not namespace or namespace == "main":
        return config
    adjusted = dict(config)
    oms_cfg = dict(adjusted.get("oms", {}) or {})
    oms_root = Path(str(oms_cfg.get("output_root", adjusted.get("paths", {}).get("oms_output_root", "")) or "")).resolve()
    if oms_root.name != namespace:
        oms_root = oms_root / namespace
    oms_cfg["output_root"] = str(oms_root)
    adjusted["oms"] = oms_cfg
    paths_cfg = dict(adjusted.get("paths", {}) or {})
    if paths_cfg.get("oms_output_root"):
        paths_cfg["oms_output_root"] = str(oms_root)
    adjusted["paths"] = paths_cfg
    return adjusted


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


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


def _candidate_funnel(summary: Dict[str, Any]) -> Dict[str, Any]:
    filt = dict(summary.get("execution_candidate_filter", {}) or {})
    tech = dict(summary.get("technical_confirmation", {}) or {})
    v2a = dict(summary.get("portfolio", {}) or {})
    kept = _safe_int(filt.get("kept_rows", 0))
    dropped = _safe_int(filt.get("dropped_rows", 0))
    allow_count = _safe_int(tech.get("allow_count", 0))
    reject_count = _safe_int(tech.get("reject_count", 0))
    selected_count = _safe_int(summary.get("n_names", 0))
    return {
        "candidate_source": _safe_text(summary.get("candidate_source")),
        "kept_rows": kept,
        "dropped_rows": dropped,
        "allow_count": allow_count,
        "reject_count": reject_count,
        "selected_count": selected_count,
        "filter_drop_ratio": round(dropped / max(kept + dropped, 1), 4),
        "technical_reject_ratio": round(reject_count / max(allow_count + reject_count, 1), 4),
        "selection_ratio_after_technical": round(selected_count / max(allow_count, 1), 4) if allow_count else 0.0,
        "v2a_state_counts": dict(v2a.get("state_counts", {}) or {}),
        "v2a_action_counts": dict(v2a.get("action_counts", {}) or {}),
    }


def _portfolio_budget(summary: Dict[str, Any], release_doc: Dict[str, Any]) -> Dict[str, Any]:
    limits = dict(summary.get("portfolio_limits", {}) or release_doc.get("constraints", {}) or {})
    weight_totals = dict(summary.get("portfolio_weight_totals", {}) or {})
    final_total = _safe_float(weight_totals.get("final_total_weight", 0.0))
    cap = _safe_float(limits.get("total_exposure_cap", 0.0))
    return {
        "total_exposure_cap": cap,
        "single_name_cap": _safe_float(limits.get("single_name_cap", 0.0)),
        "max_names": _safe_int(limits.get("max_names", 0)),
        "final_total_weight": final_total,
        "fill_ratio": round(final_total / max(cap, 1e-9), 4) if cap > 0 else 0.0,
        "target_fill": _safe_float(weight_totals.get("target_fill", 0.0)),
        "reweight_before": _safe_float(weight_totals.get("reweight_before", 0.0)),
        "reweight_after": _safe_float(weight_totals.get("reweight_after", 0.0)),
    }


def _positions_breakdown(target_df: pd.DataFrame) -> Dict[str, Any]:
    if target_df.empty:
        return {"available": False}
    frame = target_df.copy()
    if "portfolio_weight" in frame.columns:
        frame["portfolio_weight"] = pd.to_numeric(frame["portfolio_weight"], errors="coerce").fillna(0.0)
    elif "final_target_weight_v2a" in frame.columns:
        frame["portfolio_weight"] = pd.to_numeric(frame["final_target_weight_v2a"], errors="coerce").fillna(0.0)
    else:
        frame["portfolio_weight"] = 0.0
    if "industry" in frame.columns:
        top_industries = (
            frame.assign(industry=frame["industry"].fillna("unknown").astype(str))
            .groupby("industry")["portfolio_weight"]
            .sum()
            .sort_values(ascending=False)
            .head(8)
            .reset_index()
            .rename(columns={"industry": "bucket"})
            .to_dict(orient="records")
        )
    else:
        top_industries = []
    action_counts = (
        frame["position_action_intent"].astype(str).value_counts().to_dict()
        if "position_action_intent" in frame.columns
        else {}
    )
    gate_counts = (
        frame["tech_gate_reason"].astype(str).value_counts().head(10).to_dict()
        if "tech_gate_reason" in frame.columns
        else {}
    )
    return {
        "available": True,
        "n_positions": int(len(frame.index)),
        "top_industries_by_weight": top_industries,
        "action_intent_counts": action_counts,
        "tech_gate_reason_counts": gate_counts,
    }


def _prepare_target_frame(target_df: pd.DataFrame) -> pd.DataFrame:
    if target_df.empty:
        return pd.DataFrame()
    frame = target_df.copy()
    if "ts_code" in frame.columns:
        frame["symbol"] = frame["ts_code"].map(_normalize_symbol)
    elif "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    elif "code" in frame.columns:
        frame["symbol"] = frame["code"].map(_normalize_symbol)
    else:
        frame["symbol"] = ""
    if "portfolio_weight" in frame.columns:
        frame["portfolio_weight"] = pd.to_numeric(frame["portfolio_weight"], errors="coerce").fillna(0.0)
    elif "final_target_weight_v2a" in frame.columns:
        frame["portfolio_weight"] = pd.to_numeric(frame["final_target_weight_v2a"], errors="coerce").fillna(0.0)
    else:
        frame["portfolio_weight"] = 0.0
    for col, default in [
        ("mechanism_primary", "unlabeled"),
        ("primary_event_type", "unknown"),
        ("earnings_reason", "unknown"),
        ("industry", "unknown"),
        ("integrated_thesis_score", 0.0),
    ]:
        if col not in frame.columns:
            frame[col] = default
        else:
            frame[col] = frame[col].fillna(default)
    return frame


def _load_position_ledger(config: Dict[str, Any]) -> pd.DataFrame:
    try:
        path = build_oms_paths(config)["position_ledger_latest"]
    except Exception:
        return pd.DataFrame(columns=["symbol", "actual_weight", "market_value", "unrealized_pnl", "realized_pnl", "mechanism_primary"])
    frame = _read_csv(path)
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "actual_weight", "market_value", "unrealized_pnl", "realized_pnl", "mechanism_primary"])
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    else:
        frame["symbol"] = ""
    for col in ["actual_weight", "market_value", "unrealized_pnl", "realized_pnl"]:
        if col not in frame.columns:
            frame[col] = 0.0
        else:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    return frame


def _load_fill_ledger(config: Dict[str, Any]) -> pd.DataFrame:
    try:
        path = build_oms_paths(config)["fill_ledger_latest"]
    except Exception:
        return pd.DataFrame(columns=["fill_id", "order_id", "intent_id", "symbol", "side", "filled_qty", "filled_price", "filled_amount", "fee", "filled_time"])
    frame = _read_csv(path)
    if frame.empty:
        return pd.DataFrame(columns=["fill_id", "order_id", "intent_id", "symbol", "side", "filled_qty", "filled_price", "filled_amount", "fee", "filled_time"])
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    else:
        frame["symbol"] = ""
    for col in ["filled_qty", "filled_price", "filled_amount", "fee"]:
        if col not in frame.columns:
            frame[col] = 0.0
        else:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    if "side" not in frame.columns:
        frame["side"] = ""
    return frame


def _load_intent_ledger(config: Dict[str, Any]) -> pd.DataFrame:
    try:
        path = build_oms_paths(config)["intent_ledger_latest"]
    except Exception:
        return pd.DataFrame(columns=["intent_id", "symbol", "action_type", "reason", "status", "release_id"])
    frame = _read_csv(path)
    if frame.empty:
        return pd.DataFrame(columns=["intent_id", "symbol", "action_type", "reason", "status", "release_id"])
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    else:
        frame["symbol"] = ""
    return frame


def _parse_time_sort(value: Any) -> str:
    text = _safe_text(value)
    return text or "0000-00-00 00:00:00"


def _mechanism_realism_analysis(config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        path = build_oms_paths(config)["mechanism_realism_rollup"]
    except Exception:
        return {"available": False, "reason": "missing_oms_paths"}
    frame = _read_csv(path)
    if frame.empty:
        return {"available": False, "reason": f"unavailable:{path}"}
    for col in ["window_runs", "desired_count", "realized_count", "realization_ratio", "gap_pressure", "convergence_score", "non_executable_ratio"]:
        if col not in frame.columns:
            frame[col] = 0.0
        else:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    if "mechanism_primary" not in frame.columns:
        frame["mechanism_primary"] = ""
    latest_window = int(frame["window_runs"].max()) if not frame.empty else 0
    latest = frame.loc[frame["window_runs"] == latest_window].copy() if latest_window > 0 else frame.copy()
    latest = latest.sort_values(["convergence_score", "realization_ratio"], ascending=[False, False])
    return {
        "available": True,
        "path": str(path),
        "window_runs": latest_window,
        "top_realized": latest.head(6).to_dict(orient="records"),
        "top_friction": latest.sort_values(["non_executable_ratio", "gap_pressure"], ascending=[False, False]).head(6).to_dict(orient="records"),
    }


def _execution_flow_analysis(config: Dict[str, Any], target_df: pd.DataFrame) -> Dict[str, Any]:
    fills = _load_fill_ledger(config)
    intents = _load_intent_ledger(config)
    target = _prepare_target_frame(target_df)
    if fills.empty:
        return {"available": False, "reason": "missing_fill_ledger"}
    enrich_cols = [col for col in ["symbol", "mechanism_primary", "primary_event_type", "earnings_reason", "industry", "integrated_thesis_score"] if col in target.columns]
    enriched_target = target[enrich_cols].drop_duplicates(subset=["symbol"], keep="first") if not target.empty else pd.DataFrame(columns=enrich_cols)
    enriched_intents = intents[[col for col in ["intent_id", "symbol", "action_type", "reason", "status", "release_id"] if col in intents.columns]].drop_duplicates(subset=["intent_id"], keep="last") if not intents.empty else pd.DataFrame(columns=["intent_id", "symbol", "action_type", "reason", "status", "release_id"])
    merged = fills.merge(enriched_intents, on=["intent_id", "symbol"], how="left", suffixes=("", "_intent"))
    if not enriched_target.empty:
        merged = merged.merge(enriched_target, on="symbol", how="left")
    for col, default in [
        ("mechanism_primary", "unlabeled"),
        ("primary_event_type", "unknown"),
        ("earnings_reason", "unknown"),
        ("industry", "unknown"),
        ("action_type", "unknown"),
        ("filled_amount", 0.0),
        ("fee", 0.0),
        ("filled_qty", 0.0),
    ]:
        if col not in merged.columns:
            merged[col] = default
        else:
            if isinstance(default, float):
                merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(default)
            else:
                merged[col] = merged[col].fillna(default).astype(str)
    merged["signed_amount"] = merged.apply(
        lambda row: _safe_float(row.get("filled_amount", 0.0)) * (-1.0 if _safe_text(row.get("side")).lower() in {"buy", "b"} else 1.0),
        axis=1,
    )
    merged["signed_qty"] = merged.apply(
        lambda row: _safe_float(row.get("filled_qty", 0.0)) * (-1.0 if _safe_text(row.get("side")).lower() in {"buy", "b"} else 1.0),
        axis=1,
    )

    def _rollup(group_key: str) -> List[Dict[str, Any]]:
        if group_key not in merged.columns:
            return []
        bucket = (
            merged.groupby(group_key, dropna=False)
            .agg(
                gross_turnover=("filled_amount", "sum"),
                net_sell_amount=("signed_amount", "sum"),
                total_fee=("fee", "sum"),
                net_qty=("signed_qty", "sum"),
                fill_count=("fill_id", "nunique"),
            )
            .reset_index()
            .rename(columns={group_key: "bucket"})
            .sort_values("gross_turnover", ascending=False)
        )
        return bucket.to_dict(orient="records")

    top_symbols = (
        merged.groupby("symbol", dropna=False)
        .agg(
            gross_turnover=("filled_amount", "sum"),
            net_sell_amount=("signed_amount", "sum"),
            total_fee=("fee", "sum"),
            fill_count=("fill_id", "nunique"),
        )
        .reset_index()
        .sort_values("gross_turnover", ascending=False)
        .head(8)
        .to_dict(orient="records")
    )
    return {
        "available": True,
        "fill_count": int(len(merged.index)),
        "gross_turnover": round(float(merged["filled_amount"].sum()), 4),
        "net_sell_amount": round(float(merged["signed_amount"].sum()), 4),
        "total_fee": round(float(merged["fee"].sum()), 4),
        "mechanism_rollup": _rollup("mechanism_primary")[:8],
        "event_rollup": _rollup("primary_event_type")[:8],
        "action_rollup": _rollup("action_type")[:8],
        "top_symbols": top_symbols,
        "summary_lines": [
            "成交流归因反映哪些机制和标的正在被真实成交，不等于最终收益归因。",
            f"当前累计成交笔数 {int(len(merged.index))}，总成交额 {_safe_float(merged['filled_amount'].sum()):,.2f}。",
            f"当前净卖出金额 {_safe_float(merged['signed_amount'].sum()):,.2f}，累计手续费 {_safe_float(merged['fee'].sum()):,.2f}。",
        ],
    }


def _realized_pnl_approx_analysis(config: Dict[str, Any], target_df: pd.DataFrame) -> Dict[str, Any]:
    fills = _load_fill_ledger(config)
    intents = _load_intent_ledger(config)
    target = _prepare_target_frame(target_df)
    if fills.empty:
        return {"available": False, "reason": "missing_fill_ledger"}
    enrich_cols = [col for col in ["symbol", "mechanism_primary", "primary_event_type", "earnings_reason", "industry"] if col in target.columns]
    enriched_target = target[enrich_cols].drop_duplicates(subset=["symbol"], keep="first") if not target.empty else pd.DataFrame(columns=enrich_cols)
    enriched_intents = intents[[col for col in ["intent_id", "symbol", "action_type", "reason", "status", "release_id"] if col in intents.columns]].drop_duplicates(subset=["intent_id"], keep="last") if not intents.empty else pd.DataFrame(columns=["intent_id", "symbol", "action_type", "reason", "status", "release_id"])
    merged = fills.merge(enriched_intents, on=["intent_id", "symbol"], how="left", suffixes=("", "_intent"))
    if not enriched_target.empty:
        merged = merged.merge(enriched_target, on="symbol", how="left")
    if merged.empty:
        return {"available": False, "reason": "empty_fill_join"}
    for col, default in [
        ("filled_qty", 0.0),
        ("filled_price", 0.0),
        ("filled_amount", 0.0),
        ("fee", 0.0),
        ("side", ""),
        ("mechanism_primary", "unlabeled"),
        ("primary_event_type", "unknown"),
        ("earnings_reason", "unknown"),
        ("action_type", "unknown"),
    ]:
        if col not in merged.columns:
            merged[col] = default
        else:
            if isinstance(default, float):
                merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(default)
            else:
                merged[col] = merged[col].fillna(default).astype(str)
    merged["sort_key"] = merged["filled_time"].map(_parse_time_sort) if "filled_time" in merged.columns else ""
    merged = merged.sort_values(["sort_key", "fill_id"]).reset_index(drop=True)

    inventory: Dict[str, Dict[str, float]] = {}
    realized_rows: List[Dict[str, Any]] = []
    inventory_shortfall_qty = 0.0

    for row in merged.to_dict(orient="records"):
        symbol = _safe_text(row.get("symbol"))
        if not symbol:
            continue
        side = _safe_text(row.get("side")).lower()
        qty = max(_safe_float(row.get("filled_qty", 0.0)), 0.0)
        price = _safe_float(row.get("filled_price", 0.0))
        amount = _safe_float(row.get("filled_amount", 0.0))
        fee = _safe_float(row.get("fee", 0.0))
        bucket = inventory.setdefault(symbol, {"shares": 0.0, "total_cost": 0.0, "avg_cost": 0.0})
        if side in {"buy", "b"}:
            bucket["shares"] += qty
            bucket["total_cost"] += amount + fee
            bucket["avg_cost"] = bucket["total_cost"] / max(bucket["shares"], 1e-9)
            continue
        if side not in {"sell", "s"}:
            continue
        known_qty = min(bucket["shares"], qty)
        shortfall_qty = max(qty - known_qty, 0.0)
        cost_basis = bucket["avg_cost"]
        proceeds = (price * known_qty) - fee
        cost_out = cost_basis * known_qty
        realized_pnl = proceeds - cost_out
        bucket["shares"] = max(bucket["shares"] - known_qty, 0.0)
        bucket["total_cost"] = max(bucket["total_cost"] - cost_out, 0.0)
        bucket["avg_cost"] = (bucket["total_cost"] / bucket["shares"]) if bucket["shares"] > 1e-9 else 0.0
        inventory_shortfall_qty += shortfall_qty
        realized_rows.append(
            {
                "fill_id": _safe_text(row.get("fill_id")),
                "filled_time": _safe_text(row.get("filled_time")),
                "symbol": symbol,
                "mechanism_primary": _safe_text(row.get("mechanism_primary") or "unlabeled"),
                "primary_event_type": _safe_text(row.get("primary_event_type") or "unknown"),
                "earnings_reason": _safe_text(row.get("earnings_reason") or "unknown"),
                "action_type": _safe_text(row.get("action_type") or "unknown"),
                "sold_qty": round(qty, 4),
                "matched_qty": round(known_qty, 4),
                "inventory_shortfall_qty": round(shortfall_qty, 4),
                "avg_cost": round(cost_basis, 6),
                "sell_price": round(price, 6),
                "fee": round(fee, 4),
                "realized_pnl": round(realized_pnl, 4),
            }
        )

    realized_frame = pd.DataFrame(realized_rows)
    if realized_frame.empty:
        return {"available": False, "reason": "no_sell_fills"}

    def _rollup(group_key: str) -> List[Dict[str, Any]]:
        bucket = (
            realized_frame.groupby(group_key, dropna=False)
            .agg(
                realized_pnl=("realized_pnl", "sum"),
                matched_qty=("matched_qty", "sum"),
                shortfall_qty=("inventory_shortfall_qty", "sum"),
                sell_count=("fill_id", "nunique"),
            )
            .reset_index()
            .rename(columns={group_key: "bucket"})
            .sort_values("realized_pnl", ascending=False)
        )
        return bucket.to_dict(orient="records")

    top_winners = realized_frame.sort_values("realized_pnl", ascending=False).head(8).to_dict(orient="records")
    top_losers = realized_frame.sort_values("realized_pnl", ascending=True).head(8).to_dict(orient="records")
    total_realized = float(realized_frame["realized_pnl"].sum())
    return {
        "available": True,
        "mode": "fill_rebuild_approx",
        "total_realized_pnl": round(total_realized, 4),
        "sell_fill_count": int(len(realized_frame.index)),
        "inventory_shortfall_qty": round(inventory_shortfall_qty, 4),
        "mechanism_rollup": _rollup("mechanism_primary")[:8],
        "event_rollup": _rollup("primary_event_type")[:8],
        "earnings_rollup": _rollup("earnings_reason")[:8],
        "top_winners": top_winners,
        "top_losers": top_losers,
        "summary_lines": [
            f"近似已实现收益合计 {total_realized:,.2f}，基于 fill ledger 顺序重建成本。",
            f"当前纳入卖出成交 {int(len(realized_frame.index))} 笔。",
            f"库存历史缺口数量 {inventory_shortfall_qty:,.2f}；存在缺口时，该部分不会被伪装成精确已实现收益。",
        ],
    }


def _top_rows(frame: pd.DataFrame, metric: str, *, ascending: bool, limit: int = 5) -> List[Dict[str, Any]]:
    if frame.empty or metric not in frame.columns:
        return []
    bucket = frame.copy()
    bucket[metric] = pd.to_numeric(bucket[metric], errors="coerce").fillna(0.0)
    bucket = bucket.sort_values(metric, ascending=ascending).head(limit)
    return bucket.to_dict(orient="records")


def _render_attr_rows(rows: List[Dict[str, Any]], label_key: str, value_key: str, aux_key: str = "", value_mode: str = "number") -> str:
    body: List[str] = []
    for row in rows:
        label = _safe_text(row.get(label_key) or "unknown")
        value = _safe_float(row.get(value_key, 0.0))
        aux = _safe_text(row.get(aux_key)) if aux_key else ""
        extra = f"<div class='sub'>{aux}</div>" if aux else ""
        rendered_value = f"{value:,.2f}"
        if value_mode == "percent":
            rendered_value = f"{value:.2%}"
        body.append(f"<tr><td>{label}{extra}</td><td>{rendered_value}</td></tr>")
    if not body:
        body.append("<tr><td colspan='2'>当前没有可展示的归因数据。</td></tr>")
    return "".join(body)


def _pnl_source_analysis(config: Dict[str, Any], target_df: pd.DataFrame) -> Dict[str, Any]:
    target = _prepare_target_frame(target_df)
    position_ledger = _load_position_ledger(config)
    if target.empty and position_ledger.empty:
        return {"available": False, "mode": "unavailable", "reason": "missing_target_and_position_ledger"}
    merged = target.merge(position_ledger, on="symbol", how="outer", suffixes=("_target", "_actual"))
    if merged.empty:
        return {"available": False, "mode": "unavailable", "reason": "empty_join"}
    for base_col in ["mechanism_primary", "primary_event_type", "earnings_reason", "industry", "integrated_thesis_score"]:
        target_col = f"{base_col}_target"
        actual_col = f"{base_col}_actual"
        if base_col not in merged.columns:
            if target_col in merged.columns and actual_col in merged.columns:
                merged[base_col] = merged[target_col].where(merged[target_col].notna(), merged[actual_col])
            elif target_col in merged.columns:
                merged[base_col] = merged[target_col]
            elif actual_col in merged.columns:
                merged[base_col] = merged[actual_col]
    for col, default in [
        ("portfolio_weight", 0.0),
        ("actual_weight", 0.0),
        ("market_value", 0.0),
        ("unrealized_pnl", 0.0),
        ("realized_pnl", 0.0),
        ("integrated_thesis_score", 0.0),
        ("mechanism_primary", "unlabeled"),
        ("primary_event_type", "unknown"),
        ("earnings_reason", "unknown"),
        ("industry", "unknown"),
    ]:
        if col not in merged.columns:
            merged[col] = default
        else:
            if isinstance(default, float):
                merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(default)
            else:
                merged[col] = merged[col].fillna(default).astype(str)
    merged["realized_total_pnl"] = merged["realized_pnl"] + merged["unrealized_pnl"]
    live_mode = bool(position_ledger is not None and not position_ledger.empty)
    merged["attribution_weight_proxy"] = (
        merged["actual_weight"].abs() if live_mode else merged["portfolio_weight"].abs() * merged["integrated_thesis_score"].clip(lower=0.0)
    )
    mode = "live_unrealized_proxy" if live_mode else "release_weight_proxy"
    metric = "realized_total_pnl" if live_mode else "attribution_weight_proxy"
    total_metric = float(merged[metric].sum()) if live_mode else float(merged[metric].abs().sum())

    def _group_rollup(group_key: str) -> List[Dict[str, Any]]:
        if group_key not in merged.columns:
            return []
        grouped = (
            merged.groupby(group_key, dropna=False)
            .agg(
                contribution=(metric, "sum"),
                market_value=("market_value", "sum"),
                weight_proxy=("attribution_weight_proxy", "sum"),
                symbol_count=("symbol", "nunique"),
            )
            .reset_index()
            .rename(columns={group_key: "bucket"})
            .sort_values("contribution", ascending=False)
        )
        return grouped.to_dict(orient="records")

    top_winners = _top_rows(merged, metric, ascending=False, limit=5)
    top_losers = _top_rows(merged, metric, ascending=True, limit=5) if live_mode else []
    mechanism_rollup = _group_rollup("mechanism_primary")
    event_rollup = _group_rollup("primary_event_type")
    earnings_rollup = _group_rollup("earnings_reason")
    industry_rollup = _group_rollup("industry")
    strongest_mechanism = _safe_text(mechanism_rollup[0]["bucket"]) if mechanism_rollup else ""
    strongest_event = _safe_text(event_rollup[0]["bucket"]) if event_rollup else ""
    strongest_earnings = _safe_text(earnings_rollup[0]["bucket"]) if earnings_rollup else ""
    summary_lines: List[str] = []
    if strongest_mechanism:
        summary_lines.append(f"主要收益来源机制为 {strongest_mechanism}。")
    if strongest_event:
        summary_lines.append(f"领先事件类型是 {strongest_event}。")
    if strongest_earnings and strongest_earnings != "unknown":
        summary_lines.append(f"盈利验证里最占优的理由是 {strongest_earnings}。")
    if live_mode:
        summary_lines.append("当前归因基于 OMS 最新持仓浮盈亏和市值快照，属于实时代理归因，不是成交级已实现收益账本。")
    else:
        summary_lines.append("当前归因基于 release 目标权重和主线 thesis 强度，属于研究侧代理归因。")
    return {
        "available": True,
        "mode": mode,
        "metric_name": metric,
        "total_metric": round(total_metric, 4),
        "top_winners": top_winners,
        "top_losers": top_losers,
        "mechanism_rollup": mechanism_rollup[:8],
        "event_rollup": event_rollup[:8],
        "earnings_rollup": earnings_rollup[:8],
        "industry_rollup": industry_rollup[:8],
        "summary_lines": summary_lines,
    }


def _strategy_exposure(summary: Dict[str, Any], target_df: pd.DataFrame) -> Dict[str, Any]:
    state = dict(summary.get("integrated_thesis_state", {}) or {})
    thesis_summary = dict(state.get("summary", {}) or {})
    strategy_allocations = {
        "event_industry_earnings_alpha": 1.0 if state else 0.0,
        "market_risk_budget": round(_safe_float(dict(summary.get("market_state", {}) or {}).get("risk_budget_multiplier", 1.0)), 4),
    }
    output = {
        "formal_strategy_framework": _safe_text(
            summary.get("formal_strategy_framework")
            or state.get("formal_strategy_framework")
            or "integrated_event_industry_earnings_alpha"
        ),
        "primary_strategy_key": _safe_text(summary.get("primary_strategy_key") or state.get("primary_strategy_key")),
        "strategy_allocations": strategy_allocations,
        "strategy_readiness": {
            "event_industry_earnings_alpha": round(min(_safe_int(thesis_summary.get("top_candidate_count", 0)) / 12.0, 1.0), 4),
            "market_risk_budget": round(_safe_float(dict(summary.get("market_state", {}) or {}).get("risk_budget_multiplier", 1.0)), 4),
        },
    }
    if not target_df.empty and "mechanism_primary" in target_df.columns:
        frame = target_df.copy()
        if "portfolio_weight" in frame.columns:
            weights = pd.to_numeric(frame["portfolio_weight"], errors="coerce").fillna(0.0)
        elif "final_target_weight_v2a" in frame.columns:
            weights = pd.to_numeric(frame["final_target_weight_v2a"], errors="coerce").fillna(0.0)
        else:
            weights = pd.Series(0.0, index=frame.index)
        frame["portfolio_weight"] = weights
        frame["mechanism_primary"] = frame["mechanism_primary"].fillna("unlabeled").astype(str)
        mechanism_weights = (
            frame.groupby("mechanism_primary")["portfolio_weight"].sum().sort_values(ascending=False).to_dict()
        )
    else:
        mechanism_weights = {}
    output["mechanism_weight_proxy"] = {str(k): round(float(v), 6) for k, v in mechanism_weights.items()}
    return output


def _equity_curve_analysis(summary: Dict[str, Any]) -> Dict[str, Any]:
    feedback = dict(summary.get("performance_feedback", {}) or {})
    raw = _safe_text(feedback.get("source_equity_curve"))
    if not raw:
        return {"available": False, "reason": "missing_equity_curve_path"}
    path = Path(raw)
    frame = _read_csv(path)
    if frame.empty or "nav" not in frame.columns:
        return {"available": False, "reason": f"unavailable:{path}"}
    bucket = frame.copy()
    ts_col = "timestamp" if "timestamp" in bucket.columns else bucket.columns[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        bucket[ts_col] = pd.to_datetime(bucket[ts_col], errors="coerce")
    bucket["nav"] = pd.to_numeric(bucket["nav"], errors="coerce")
    bucket = bucket.dropna(subset=[ts_col, "nav"]).sort_values(ts_col)
    if bucket.empty:
        return {"available": False, "reason": f"empty:{path}"}
    latest_nav = float(bucket["nav"].iloc[-1])
    peak = bucket["nav"].cummax()
    drawdown = bucket["nav"] / peak - 1.0
    result = {
        "available": True,
        "path": str(path),
        "latest_nav": latest_nav,
        "current_drawdown": round(float(drawdown.iloc[-1]), 6),
        "max_drawdown": round(float(drawdown.min()), 6),
    }
    for horizon in (1, 5, 20):
        if len(bucket.index) > horizon:
            prev = float(bucket["nav"].iloc[-1 - horizon])
            result[f"ret_{horizon}d"] = round(latest_nav / prev - 1.0, 6) if prev > 0 else 0.0
        else:
            result[f"ret_{horizon}d"] = 0.0
    result["series"] = [
        {"date": str(bucket[ts_col].iloc[idx])[:10], "nav": round(float(bucket["nav"].iloc[idx]), 6)}
        for idx in range(max(0, len(bucket.index) - 20), len(bucket.index))
    ]
    return result


def _benchmark_comparison(config: Dict[str, Any], equity: Dict[str, Any]) -> Dict[str, Any]:
    market_cfg = dict(config.get("market_pipeline", {}) or {})
    hs300_path = Path(str(market_cfg.get("hs300_path", "") or config.get("hs300_path", "") or "")).resolve()
    if not hs300_path.exists():
        return {"available": False, "reason": f"missing_benchmark:{hs300_path}"}
    frame = _read_csv(hs300_path)
    if frame.empty or "date" not in frame.columns or "close" not in frame.columns:
        return {"available": False, "reason": f"unavailable:{hs300_path}"}
    bench = frame.copy()
    bench["date"] = pd.to_datetime(bench["date"], errors="coerce")
    bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
    bench = bench.dropna(subset=["date", "close"]).sort_values("date")
    if bench.empty:
        return {"available": False, "reason": f"empty:{hs300_path}"}
    result = {
        "available": True,
        "path": str(hs300_path),
        "latest_close": round(float(bench["close"].iloc[-1]), 6),
    }
    for horizon in (1, 5, 20, 60):
        if len(bench.index) > horizon:
            prev = float(bench["close"].iloc[-1 - horizon])
            latest = float(bench["close"].iloc[-1])
            result[f"ret_{horizon}d"] = round(latest / prev - 1.0, 6) if prev > 0 else 0.0
        else:
            result[f"ret_{horizon}d"] = 0.0
    if equity.get("available") and list(equity.get("series", []) or []):
        eq = pd.DataFrame(list(equity.get("series", []) or []))
        if not eq.empty and "date" in eq.columns and "nav" in eq.columns:
            eq["date"] = pd.to_datetime(eq["date"], errors="coerce")
            eq["nav"] = pd.to_numeric(eq["nav"], errors="coerce")
            eq = eq.dropna(subset=["date", "nav"]).sort_values("date")
            overlap = eq.merge(bench[["date", "close"]], on="date", how="inner")
            if not overlap.empty:
                first_nav = float(overlap["nav"].iloc[0])
                first_close = float(overlap["close"].iloc[0])
                if first_nav > 0 and first_close > 0:
                    overlap["system_norm"] = overlap["nav"] / first_nav
                    overlap["benchmark_norm"] = overlap["close"] / first_close
                    result["excess_return_since_overlap_start"] = round(
                        float(overlap["system_norm"].iloc[-1] - overlap["benchmark_norm"].iloc[-1]),
                        6,
                    )
                    result["comparison_series"] = [
                        {
                            "date": str(overlap["date"].iloc[idx])[:10],
                            "system_norm": round(float(overlap["system_norm"].iloc[idx]), 6),
                            "benchmark_norm": round(float(overlap["benchmark_norm"].iloc[idx]), 6),
                        }
                        for idx in range(max(0, len(overlap.index) - 30), len(overlap.index))
                    ]
                    for horizon in (5, 20):
                        eq_key = f"ret_{horizon}d"
                        bench_key = f"ret_{horizon}d"
                        result[f"excess_{horizon}d"] = round(
                            _safe_float(equity.get(eq_key, 0.0)) - _safe_float(result.get(bench_key, 0.0)),
                            6,
                        )
    return result


def _actual_state_analysis(config: Dict[str, Any]) -> Dict[str, Any]:
    oms_root = Path(
        str(config.get("paths", {}).get("oms_output_root", "") or config.get("oms", {}).get("output_root", "") or "")
    ).resolve()
    if not str(oms_root).strip():
        return {"available": False, "reason": "missing_oms_root"}
    latest_actual = oms_root / "snapshots" / "latest_actual_portfolio_state.json"
    payload = _load_json(latest_actual)
    if not payload:
        return {"available": False, "reason": f"unavailable:{latest_actual}"}
    positions = list(payload.get("positions", []) or [])
    mechanism_counts: Dict[str, int] = {}
    gap_weight_abs = 0.0
    for row in positions:
        mechanism = _safe_text(dict(row).get("mechanism_primary") or "unlabeled")
        mechanism_counts[mechanism] = mechanism_counts.get(mechanism, 0) + 1
        gap_weight_abs += abs(_safe_float(dict(row).get("gap_weight_abs", 0.0)))
    return {
        "available": True,
        "release_id": _safe_text(payload.get("release_id")),
        "n_positions": len(positions),
        "account_total_asset": _safe_float(dict(payload.get("account", {}) or {}).get("total_asset", 0.0)),
        "account_cash": _safe_float(dict(payload.get("account", {}) or {}).get("cash", 0.0)),
        "actual_state_counts": dict(dict(payload.get("summary", {}) or {}).get("actual_state_counts", {}) or {}),
        "mechanism_counts": mechanism_counts,
        "gap_weight_abs_sum": round(gap_weight_abs, 6),
    }


def _overfit_risk(summary: Dict[str, Any], funnel: Dict[str, Any], budget: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    score = 0
    total_score = _safe_float(summary.get("total_score", 0.0))
    sharpe = _safe_float(summary.get("sharpe", 0.0))
    if "fallback" in _safe_text(funnel.get("candidate_source")).lower():
        score += 1
        reasons.append("portfolio_used_latest_scores_fallback")
    if _safe_float(funnel.get("filter_drop_ratio", 0.0)) >= 0.25:
        score += 1
        reasons.append("candidate_universe_filter_drop_high")
    if _safe_float(funnel.get("technical_reject_ratio", 0.0)) >= 0.70:
        score += 1
        reasons.append("technical_reject_ratio_high")
    if _safe_float(budget.get("fill_ratio", 0.0)) <= 0.75:
        score += 1
        reasons.append("post_filter_fill_ratio_low")
    if total_score >= 35.0 and sharpe >= 1.2 and _safe_float(funnel.get("technical_reject_ratio", 0.0)) >= 0.60:
        score += 1
        reasons.append("paper_metrics_ok_but_live_admission_friction_high")
    level = "low"
    if score >= 4:
        level = "high"
    elif score >= 2:
        level = "medium"
    return {
        "risk_level": level,
        "risk_score": score,
        "reasons": reasons,
        "interpretation": {
            "low": "Current evidence does not show a strong paper-to-live mismatch.",
            "medium": "There are visible signs that candidate quality or live filters are compressing deployable alpha.",
            "high": "Model or candidate quality likely looks better on paper than in live admission or execution conditions.",
        }[level],
    }


def _plain_language(
    summary: Dict[str, Any],
    strategy_exposure: Dict[str, Any],
    funnel: Dict[str, Any],
    overfit: Dict[str, Any],
    equity: Dict[str, Any],
    benchmark: Dict[str, Any],
    actual: Dict[str, Any],
    pnl_source: Dict[str, Any],
    mechanism_realism: Dict[str, Any],
    execution_flow: Dict[str, Any],
    realized_pnl: Dict[str, Any],
    t_overlay: Dict[str, Any],
    tactical: Dict[str, Any],
) -> Dict[str, Any]:
    strengths: List[str] = []
    drags: List[str] = []
    primary = _safe_text(strategy_exposure.get("primary_strategy_key"))
    if primary:
        strengths.append(f"Current primary strategy is {primary}.")
    if equity.get("available"):
        ret_5d = _safe_float(equity.get("ret_5d", 0.0))
        if ret_5d > 0:
            strengths.append(f"Recent 5-day NAV trend is positive at {ret_5d:.2%}.")
        elif ret_5d < 0:
            drags.append(f"Recent 5-day NAV trend is negative at {ret_5d:.2%}.")
    if benchmark.get("available") and "excess_20d" in benchmark:
        excess_20d = _safe_float(benchmark.get("excess_20d", 0.0))
        if excess_20d > 0:
            strengths.append(f"System outperformed HS300 by {excess_20d:.2%} over the recent 20-day comparison window.")
        elif excess_20d < 0:
            drags.append(f"System underperformed HS300 by {abs(excess_20d):.2%} over the recent 20-day comparison window.")
    if _safe_float(funnel.get("technical_reject_ratio", 0.0)) > 0.60:
        drags.append("Technical confirmation reject ratio is high, so many candidates never reached the final portfolio.")
    if _safe_float(funnel.get("filter_drop_ratio", 0.0)) > 0.20:
        drags.append("Too much candidate loss happened in the tradable-universe filter, which weakens paper-to-live conversion.")
    if actual.get("available") and _safe_float(actual.get("gap_weight_abs_sum", 0.0)) > 0:
        drags.append("OMS actual positions still show a residual gap versus target positions.")
    if overfit.get("risk_level") in {"medium", "high"}:
        drags.append(f"There is a {overfit.get('risk_level')} overfit or paper-to-live mismatch warning.")
    if pnl_source.get("available"):
        for line in list(pnl_source.get("summary_lines", []) or [])[:3]:
            strengths.append(line)
    if mechanism_realism.get("available"):
        top_realized = list(mechanism_realism.get("top_realized", []) or [])
        top_friction = list(mechanism_realism.get("top_friction", []) or [])
        if top_realized:
            strengths.append(
                f"机制落地效率最高的是 {_safe_text(top_realized[0].get('mechanism_primary'))}，实现率 {_safe_float(top_realized[0].get('realization_ratio')):.2%}。"
            )
        if top_friction:
            drags.append(
                f"当前落地摩擦最大的机制是 {_safe_text(top_friction[0].get('mechanism_primary'))}，不可执行比例 {_safe_float(top_friction[0].get('non_executable_ratio')):.2%}。"
            )
    if execution_flow.get("available"):
        for line in list(execution_flow.get("summary_lines", []) or [])[:2]:
            strengths.append(line)
    if realized_pnl.get("available"):
        for line in list(realized_pnl.get("summary_lines", []) or [])[:2]:
            strengths.append(line)
    if t_overlay.get("available"):
        for line in list(t_overlay.get("summary_lines", []) or [])[:2]:
            strengths.append(line)
    if tactical.get("available"):
        for line in list(tactical.get("summary_lines", []) or [])[:2]:
            strengths.append(line)
    if not strengths:
        strengths.append("No strong positive driver is available yet because realized PnL attribution history is still incomplete.")
    if not drags:
        drags.append("No major drag is visible in the current proxy-based audit view.")
    return {
        "what_helped": strengths,
        "what_hurt": drags,
    }


def _bar(label: str, value: float, color: str) -> str:
    pct = max(0.0, min(100.0, float(value) * 100.0))
    return (
        f"<div class='bar-row'><div class='bar-label'>{label}</div>"
        f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%;background:{color};'></div></div>"
        f"<div class='bar-value'>{pct:.1f}%</div></div>"
    )


def _line_chart(series: List[Dict[str, Any]]) -> str:
    if not series:
        return "<div>当前没有可用的时间序列对比。</div>"
    values: List[float] = []
    for row in series:
        for key in ("nav", "system_norm", "benchmark_norm"):
            if key in row:
                values.append(_safe_float(row.get(key, 0.0)))
    if not values:
        return "<div>当前没有可用的时间序列对比。</div>"
    min_v = min(values)
    max_v = max(values)
    spread = max(max_v - min_v, 1e-9)

    def build_points(key: str) -> str:
        points: List[str] = []
        for idx, row in enumerate(series):
            x = 20 + (760 * idx / max(len(series) - 1, 1))
            y = 180 - (( _safe_float(row.get(key, 0.0)) - min_v) / spread) * 140
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)

    system_points = build_points("system_norm" if "system_norm" in series[0] else "nav")
    benchmark_points = build_points("benchmark_norm") if "benchmark_norm" in series[0] else ""
    labels = "".join(
        f"<div class='axis-label' style='left:{20 + (760 * idx / max(len(series) - 1, 1)):.1f}px'>{_safe_text(row.get('date'))}</div>"
        for idx, row in enumerate(series[:: max(len(series) // 5, 1)])
    )
    svg = [
        "<div class='chart-wrap'>",
        "<svg viewBox='0 0 800 220' class='line-chart'>",
        "<line x1='20' y1='180' x2='780' y2='180' class='axis' />",
        f"<polyline fill='none' stroke='#2f7f66' stroke-width='3' points='{system_points}' />",
    ]
    if benchmark_points:
        svg.append(f"<polyline fill='none' stroke='#28638b' stroke-width='3' stroke-dasharray='6 4' points='{benchmark_points}' />")
    svg.append("</svg>")
    svg.append(labels)
    svg.append("</div>")
    return "".join(svg)


def _render_html(payload: Dict[str, Any]) -> str:
    strategy_allocations = dict(payload.get("strategy_exposure", {}).get("strategy_allocations", {}) or {})
    funnel = dict(payload.get("candidate_funnel", {}) or {})
    budget = dict(payload.get("portfolio_budget", {}) or {})
    overfit = dict(payload.get("overfit_risk", {}) or {})
    plain = dict(payload.get("plain_language", {}) or {})
    equity = dict(payload.get("equity_curve_analysis", {}) or {})
    benchmark = dict(payload.get("benchmark_comparison", {}) or {})
    actual = dict(payload.get("actual_state_analysis", {}) or {})
    pnl_source = dict(payload.get("pnl_source_analysis", {}) or {})
    mechanism_realism = dict(payload.get("mechanism_realism_analysis", {}) or {})
    execution_flow = dict(payload.get("execution_flow_analysis", {}) or {})
    realized_pnl = dict(payload.get("realized_pnl_analysis", {}) or {})
    t_overlay = dict(payload.get("t_overlay_analysis", {}) or {})
    tactical = dict(payload.get("intraday_tactical_analysis", {}) or {})
    top_industries = list(dict(payload.get("positions_breakdown", {}) or {}).get("top_industries_by_weight", []) or [])
    chart_series = list(benchmark.get("comparison_series", []) or equity.get("series", []) or [])
    html = [
        "<html><head><meta charset='utf-8'><title>策略审计</title>",
        "<style>",
        "body{font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f4f1ea;color:#1f2a30;margin:0;padding:24px;}",
        ".wrap{max-width:1100px;margin:0 auto;}",
        ".hero{background:linear-gradient(135deg,#fdf8ef,#e5efe9);border:1px solid #d9e4dc;border-radius:18px;padding:24px;}",
        ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-top:18px;}",
        ".grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:18px;}",
        ".card{background:#fffdf8;border:1px solid #e5ddd1;border-radius:16px;padding:18px;box-shadow:0 8px 24px rgba(31,42,48,.05);}",
        ".bar-row{display:grid;grid-template-columns:200px 1fr 70px;gap:10px;align-items:center;margin:8px 0;}",
        ".bar-track{height:12px;background:#ece7dc;border-radius:999px;overflow:hidden;}",
        ".bar-fill{height:100%;border-radius:999px;}",
        ".kv{display:grid;grid-template-columns:180px 1fr;gap:6px 12px;font-size:14px;}",
        ".pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#1f2a30;color:#fff;font-size:12px;}",
        ".metric{font-size:32px;font-weight:700;margin:6px 0;}",
        ".sub{font-size:13px;color:#61717a;}",
        ".line-chart{width:100%;height:220px;display:block;}",
        ".axis{stroke:#c7c0b4;stroke-width:1;}",
        ".chart-wrap{position:relative;padding-bottom:18px;}",
        ".axis-label{position:absolute;bottom:0;transform:translateX(-50%);font-size:11px;color:#7a6f64;white-space:nowrap;}",
        "ul{margin:8px 0 0 18px;padding:0;} table{width:100%;border-collapse:collapse;font-size:14px;} th,td{padding:8px;border-bottom:1px solid #eee5d9;text-align:left;}",
        "@media (max-width: 900px){.grid,.grid-3{grid-template-columns:1fr;}.bar-row{grid-template-columns:120px 1fr 60px;}}",
        "</style></head><body><div class='wrap'>",
        f"<div class='hero'><h1>策略审计</h1><div class='pill'>{_safe_text(payload.get('trade_date'))}</div>",
        f"<p>主导策略：<strong>{_safe_text(payload.get('strategy_exposure', {}).get('primary_strategy_key')) or 'unknown'}</strong></p>",
        f"<p>过拟合风险：<strong>{_safe_text(overfit.get('risk_level')).upper()}</strong> | 解释：{_safe_text(overfit.get('interpretation'))}</p></div>",
        "<div class='grid-3'>",
        f"<div class='card'><h2>系统净值</h2><div class='metric'>{_safe_float(equity.get('latest_nav', 0.0)):.3f}</div><div class='sub'>当前回撤 {_safe_float(equity.get('current_drawdown', 0.0)):.2%}</div></div>",
        f"<div class='card'><h2>20日相对沪深300超额</h2><div class='metric'>{_safe_float(benchmark.get('excess_20d', 0.0)):.2%}</div><div class='sub'>系统 20日 {_safe_float(equity.get('ret_20d', 0.0)):.2%}，沪深300 {_safe_float(benchmark.get('ret_20d', 0.0)):.2%}</div></div>",
        f"<div class='card'><h2>账户资产</h2><div class='metric'>{_safe_float(actual.get('account_total_asset', 0.0)):.0f}</div><div class='sub'>现金 {_safe_float(actual.get('account_cash', 0.0)):.0f}</div></div>",
        "</div>",
        "<div class='card' style='margin-top:18px;'><h2>系统与基准对比</h2>",
        _line_chart(chart_series),
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>钱从哪来</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(pnl_source.get("summary_lines", []) or [])[:6]) + "</ul>"
        + f"<div class='sub'>归因模式：{_safe_text(pnl_source.get('mode')) or 'unknown'}</div></div>",
        "<div class='card'><h2>按机制归因</h2><table><thead><tr><th>机制</th><th>贡献</th></tr></thead><tbody>"
        + _render_attr_rows(list(pnl_source.get("mechanism_rollup", []) or []), "bucket", "contribution")
        + "</tbody></table></div>",
        "<div class='card'><h2>按事件归因</h2><table><thead><tr><th>事件</th><th>贡献</th></tr></thead><tbody>"
        + _render_attr_rows(list(pnl_source.get("event_rollup", []) or []), "bucket", "contribution")
        + "</tbody></table></div>",
        "<div class='card'><h2>按盈利验证归因</h2><table><thead><tr><th>盈利理由</th><th>贡献</th></tr></thead><tbody>"
        + _render_attr_rows(list(pnl_source.get("earnings_rollup", []) or []), "bucket", "contribution")
        + "</tbody></table></div>",
        "<div class='card'><h2>主要盈利标的</h2><table><thead><tr><th>标的</th><th>贡献</th></tr></thead><tbody>"
        + _render_attr_rows(list(pnl_source.get("top_winners", []) or []), "symbol", pnl_source.get("metric_name") or "realized_total_pnl", "mechanism_primary")
        + "</tbody></table></div>",
        "<div class='card'><h2>主要拖累标的</h2><table><thead><tr><th>标的</th><th>贡献</th></tr></thead><tbody>"
        + _render_attr_rows(list(pnl_source.get("top_losers", []) or []), "symbol", pnl_source.get("metric_name") or "realized_total_pnl", "mechanism_primary")
        + "</tbody></table></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>近似已实现收益摘要</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(realized_pnl.get("summary_lines", []) or [])[:6]) + "</ul></div>",
        "<div class='card'><h2>按机制已实现收益</h2><table><thead><tr><th>机制</th><th>收益</th></tr></thead><tbody>"
        + _render_attr_rows(list(realized_pnl.get("mechanism_rollup", []) or []), "bucket", "realized_pnl")
        + "</tbody></table></div>",
        "<div class='card'><h2>按事件已实现收益</h2><table><thead><tr><th>事件</th><th>收益</th></tr></thead><tbody>"
        + _render_attr_rows(list(realized_pnl.get("event_rollup", []) or []), "bucket", "realized_pnl")
        + "</tbody></table></div>",
        "<div class='card'><h2>主要已实现盈利/亏损</h2><table><thead><tr><th>标的</th><th>收益</th></tr></thead><tbody>"
        + _render_attr_rows(list(realized_pnl.get("top_winners", []) or []), "symbol", "realized_pnl", "mechanism_primary")
        + "</tbody></table></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>T 执行审计摘要</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(t_overlay.get("summary_lines", []) or [])[:6]) + "</ul></div>",
        "<div class='card'><h2>T 按窗口</h2><table><thead><tr><th>窗口</th><th>触发 / 执行</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_safe_text(item.get('timing_window') or item.get('bucket'))}</td><td>{_safe_int(item.get('t_triggered_symbols', item.get('trigger_count', 0)))} / {_safe_int(item.get('execution_count', 0))}</td></tr>"
            for item in list(t_overlay.get("window_daily", []) or [])[:6]
        )
        + "</tbody></table></div>",
        "<div class='card'><h2>T 主要阻断原因</h2><table><thead><tr><th>原因</th><th>样本</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_safe_text(item.get('reject_reason') or item.get('bucket'))}</td><td>{_safe_int(item.get('sample_count', item.get('reject_count', 0)))}</td></tr>"
            for item in list(t_overlay.get("reject_reason_summary", []) or [])[:6]
        )
        + "</tbody></table></div>",
        "<div class='card'><h2>T 机制适配</h2><table><thead><tr><th>机制</th><th>触发 / 执行</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_safe_text(item.get('bucket'))}</td><td>{_safe_int(item.get('trigger_count', 0))} / {_safe_int(item.get('execution_count', 0))}</td></tr>"
            for item in list(t_overlay.get("mechanism_summary", []) or [])[:6]
        )
        + "</tbody></table></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>盘中战术摘要</h2><ul>"
        + "".join(f"<li>{_safe_text(item)}</li>" for item in list(tactical.get("summary_lines", []) or [])[:8])
        + "</ul>"
        + (
            f"<div class='sub'>加仓 {_safe_int(tactical.get('add_order_count'))} / 减仓 {_safe_int(tactical.get('reduce_order_count'))}"
            f" · 同标的冲突组 {_safe_int(tactical.get('n_symbol_conflicts'))}</div>"
            if tactical.get("available")
            else "<div class='sub'>暂无盘中战术产物或路径未就绪。</div>"
        )
        + "</div>",
        "<div class='card'><h2>战术 reason_code</h2><table><thead><tr><th>原因码</th><th>次数</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_safe_text(item.get('reason_code'))}</td><td>{_safe_int(item.get('count'))}</td></tr>"
            for item in list(tactical.get("reason_code_counts", []) or [])[:10]
        )
        + "</tbody></table></div>",
        "<div class='card'><h2>战术冲突仲裁</h2><table><thead><tr><th>标的</th><th>胜出意图</th><th>压制条数</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_safe_text(item.get('symbol'))}</td><td>{_safe_text(item.get('winner_intent_id'))}</td>"
            f"<td>{_safe_int(item.get('n_suppressed'))}</td></tr>"
            for item in list(tactical.get("conflict_summary", []) or [])[:10]
        )
        + "</tbody></table></div>",
        "<div class='card'><h2>意图压制</h2><ul>"
        + "".join(f"<li>{_safe_text(item)}</li>" for item in list(tactical.get("block_reasons", []) or [])[:10])
        + "</ul></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>成交流归因摘要</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(execution_flow.get("summary_lines", []) or [])[:6]) + "</ul></div>",
        "<div class='card'><h2>按机制成交</h2><table><thead><tr><th>机制</th><th>成交额</th></tr></thead><tbody>"
        + _render_attr_rows(list(execution_flow.get("mechanism_rollup", []) or []), "bucket", "gross_turnover")
        + "</tbody></table></div>",
        "<div class='card'><h2>按动作成交</h2><table><thead><tr><th>动作</th><th>成交额</th></tr></thead><tbody>"
        + _render_attr_rows(list(execution_flow.get("action_rollup", []) or []), "bucket", "gross_turnover")
        + "</tbody></table></div>",
        "<div class='card'><h2>高成交标的</h2><table><thead><tr><th>标的</th><th>成交额</th></tr></thead><tbody>"
        + _render_attr_rows(list(execution_flow.get("top_symbols", []) or []), "symbol", "gross_turnover")
        + "</tbody></table></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>机制实现率</h2><table><thead><tr><th>机制</th><th>实现率</th></tr></thead><tbody>"
        + _render_attr_rows(list(mechanism_realism.get("top_realized", []) or []), "mechanism_primary", "realization_ratio", value_mode="percent")
        + "</tbody></table></div>",
        "<div class='card'><h2>机制落地摩擦</h2><table><thead><tr><th>机制</th><th>不可执行比例</th></tr></thead><tbody>"
        + _render_attr_rows(list(mechanism_realism.get("top_friction", []) or []), "mechanism_primary", "non_executable_ratio", value_mode="percent")
        + "</tbody></table></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>策略分配</h2>",
        _bar("主线事件产业盈利 Alpha", _safe_float(strategy_allocations.get("event_industry_earnings_alpha", 0.0)), "#cf6a32"),
        _bar("市场风险预算", _safe_float(strategy_allocations.get("market_risk_budget", 0.0)), "#28638b"),
        "</div>",
        "<div class='card'><h2>落地漏斗</h2>",
        _bar("候选过滤流失", _safe_float(funnel.get("filter_drop_ratio", 0.0)), "#a94c4c"),
        _bar("技术确认拒绝", _safe_float(funnel.get("technical_reject_ratio", 0.0)), "#8b6b24"),
        _bar("仓位填充率", _safe_float(budget.get("fill_ratio", 0.0)), "#4d7f38"),
        "</div>",
        "<div class='card'><h2>正向因素</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(plain.get("what_helped", []) or [])[:8]) + "</ul></div>",
        "<div class='card'><h2>拖累因素</h2><ul>" + "".join(f"<li>{_safe_text(item)}</li>" for item in list(plain.get("what_hurt", []) or [])[:8]) + "</ul></div>",
        "</div>",
        "<div class='grid'>",
        "<div class='card'><h2>关键指标</h2><div class='kv'>",
        f"<div>候选来源</div><div>{_safe_text(funnel.get('candidate_source'))}</div>",
        f"<div>保留 / 剔除</div><div>{_safe_int(funnel.get('kept_rows'))} / {_safe_int(funnel.get('dropped_rows'))}</div>",
        f"<div>允许 / 拒绝</div><div>{_safe_int(funnel.get('allow_count'))} / {_safe_int(funnel.get('reject_count'))}</div>",
        f"<div>最终总仓位</div><div>{_safe_float(budget.get('final_total_weight')):.2%}</div>",
        f"<div>仓位上限</div><div>{_safe_float(budget.get('total_exposure_cap')):.2%}</div>",
        f"<div>过拟合原因</div><div>{', '.join(list(overfit.get('reasons', []) or [])) or 'none'}</div>",
        "</div></div>",
        "<div class='card'><h2>重点行业</h2><table><thead><tr><th>行业</th><th>权重</th></tr></thead><tbody>",
        "".join(
            f"<tr><td>{_safe_text(item.get('industry') or item.get('bucket'))}</td><td>{_safe_float(item.get('portfolio_weight', item.get('weight', 0.0))):.2%}</td></tr>"
            for item in top_industries[:8]
        ),
        "</tbody></table></div>",
        "</div>",
        "</div></body></html>",
    ]
    return "".join(html)


def build_strategy_audit_pack(
    config: Dict[str, Any],
    *,
    trade_date: str,
    release_doc: Dict[str, Any],
    pack_dir: Path,
) -> Dict[str, Any]:
    pack_dir = ensure_dir(pack_dir)
    audit_config = _audit_config_for_release(config=config, release_doc=release_doc)
    release_artifacts = dict(release_doc.get("artifacts", {}) or {})
    summary_path = Path(_safe_text(release_artifacts.get("portfolio_summary_path")))
    target_path = Path(_safe_text(release_artifacts.get("target_positions_path")))
    summary = _load_json(summary_path) if summary_path.exists() else {}
    target_df = _read_csv(target_path) if target_path.exists() else pd.DataFrame()

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    funnel = _candidate_funnel(summary)
    budget = _portfolio_budget(summary, release_doc)
    positions = _positions_breakdown(target_df)
    strategy_exposure = _strategy_exposure(summary, target_df)
    equity = _equity_curve_analysis(summary)
    actual = _actual_state_analysis(audit_config)
    pnl_source = _pnl_source_analysis(audit_config, target_df)
    alpha_attribution = build_alpha_attribution(target_df, _load_position_ledger(audit_config))
    alpha_lifecycle = dict(summary.get("alpha_lifecycle", {}) or {})
    trade_discipline = dict(summary.get("trade_discipline", {}) or {})
    llm_operating_brain = dict(summary.get("llm_operating_brain", {}) or {})
    mechanism_realism = _mechanism_realism_analysis(audit_config)
    execution_flow = _execution_flow_analysis(audit_config, target_df)
    realized_pnl = _realized_pnl_approx_analysis(audit_config, target_df)
    t_overlay = build_t_audit_pack(audit_config, trade_date=trade_date, release_doc=release_doc, pack_dir=pack_dir)
    tactical = build_intraday_tactical_audit_pack(audit_config)
    benchmark = _benchmark_comparison(audit_config, equity)
    overfit = _overfit_risk(summary, funnel, budget)
    plain = _plain_language(
        summary,
        strategy_exposure,
        funnel,
        overfit,
        equity,
        benchmark,
        actual,
        pnl_source,
        mechanism_realism,
        execution_flow,
        realized_pnl,
        t_overlay,
        tactical,
    )

    payload = {
        "generated_at": generated_at,
        "trade_date": trade_date,
        "release_id": _safe_text(release_doc.get("release_id")),
        "strategy_exposure": strategy_exposure,
        "candidate_funnel": funnel,
        "portfolio_budget": budget,
        "positions_breakdown": positions,
        "equity_curve_analysis": equity,
        "benchmark_comparison": benchmark,
        "actual_state_analysis": actual,
        "pnl_source_analysis": pnl_source,
        "alpha_attribution": alpha_attribution,
        "alpha_lifecycle_analysis": alpha_lifecycle,
        "trade_discipline": trade_discipline,
        "llm_operating_brain": llm_operating_brain,
        "mechanism_realism_analysis": mechanism_realism,
        "execution_flow_analysis": execution_flow,
        "realized_pnl_analysis": realized_pnl,
        "t_overlay_analysis": t_overlay,
        "intraday_tactical_analysis": tactical,
        "overfit_risk": overfit,
        "plain_language": plain,
        "limitations": [
            "realized_pnl_by_strategy_requires_position_level_strategy_tags_plus_historical_nav_or_fill_pnl",
            "current_audit_uses_release_and_execution_proxies_when_full_pnl_ledger_is_missing",
            "industry_vs_earnings_strategy_exposure_is_partial_when_position_rows_do_not_carry_explicit_strategy_tags",
            "money_source_analysis_is_live_unrealized_proxy_when_only_position_ledger_is_available",
            "execution_flow_analysis_measures_real_trading_flow_not_final_realized_profit",
            "realized_pnl_analysis_is_approximate_when_fill_history_is incomplete_or_inventory_cost_history_starts_midstream",
        ],
    }
    payload["plain_language"]["alpha_lifecycle_lines"] = summarize_alpha_lifecycle_lines(alpha_lifecycle)
    if trade_discipline:
        payload["plain_language"]["trade_discipline_lines"] = [
            f"posture={_safe_text(trade_discipline.get('posture')) or 'balanced'} cash_posture={_safe_text(trade_discipline.get('cash_posture')) or 'hold_buffer'}",
            f"new_position_budget={_safe_float(trade_discipline.get('new_position_budget'), 0.0):.2f} add_multiplier={_safe_float(trade_discipline.get('add_multiplier'), 0.0):.2f} sell_pressure={_safe_float(trade_discipline.get('sell_pressure'), 0.0):.2f}",
        ]
    json_path = pack_dir / "strategy_audit.json"
    html_path = pack_dir / "strategy_audit.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_render_html(payload), encoding="utf-8")
    return {
        "generated_at": generated_at,
        "json_path": str(json_path),
        "html_path": str(html_path),
        "payload": payload,
    }
