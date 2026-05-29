from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _load_json(path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    from .sql_store import load_runtime_json_prefer_sql

    return load_runtime_json_prefer_sql(config, path, default={})


def _repo_data_root(config: Dict[str, Any]) -> Path:
    raw = _safe_text((config.get("paths") or {}).get("data_root", ""))
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[3] / "data"


def build_intraday_tactical_audit_pack(config: Dict[str, Any]) -> Dict[str, Any]:
    root = _repo_data_root(config)
    tactics_latest = root / "trade_clock" / "intraday_tactics" / "latest"
    audit_latest = root / "audit_v1" / "latest" / "latest_intraday_tactical_audit.json"

    summary = _load_json(tactics_latest / "intraday_tactical_summary.json", config)
    intents_doc = _load_json(tactics_latest / "intraday_action_intents.json", config)
    orders_doc = _load_json(tactics_latest / "intraday_tactical_orders.json", config)
    conflicts_doc = _load_json(tactics_latest / "intraday_tactical_conflicts.json", config)
    audit_doc = _load_json(audit_latest, config)

    intents = list(intents_doc.get("intents") or [])
    orders = list(orders_doc.get("orders") or [])
    conflicts = list(conflicts_doc.get("conflicts") or [])
    suppressed = list(conflicts_doc.get("suppressed") or [])

    add_count = sum(1 for row in orders if _safe_text(row.get("side")).upper() == "BUY")
    reduce_count = sum(1 for row in orders if _safe_text(row.get("side")).upper() == "SELL")
    kept_ratio = round(len(orders) / max(len(intents), 1), 4) if intents else 0.0
    reduce_ratio = round(reduce_count / max(len(orders), 1), 4) if orders else 0.0

    reason_counts: Dict[str, int] = {}
    role_counts: Dict[str, int] = {}
    side_counts: Dict[str, int] = {}
    alpha_family_counts: Dict[str, int] = {}
    for row in intents:
        payload = dict(row.get("debug_payload") or {})
        reason_code = _safe_text(row.get("reason_code"))
        role = _safe_text(payload.get("portfolio_service_role"))
        side = _safe_text(row.get("side")).upper()
        alpha_family = _safe_text(payload.get("alpha_family"))
        if reason_code:
            reason_counts[reason_code] = int(reason_counts.get(reason_code, 0) or 0) + 1
        if role:
            role_counts[role] = int(role_counts.get(role, 0) or 0) + 1
        if side:
            side_counts[side] = int(side_counts.get(side, 0) or 0) + 1
        if alpha_family:
            alpha_family_counts[alpha_family] = int(alpha_family_counts.get(alpha_family, 0) or 0) + 1

    merged_audit_reasons = dict(audit_doc.get("reason_code_counts") or {})
    for key, value in merged_audit_reasons.items():
        reason = _safe_text(key)
        if not reason:
            continue
        try:
            reason_counts[reason] = max(int(reason_counts.get(reason, 0) or 0), int(value or 0))
        except Exception:
            reason_counts[reason] = int(reason_counts.get(reason, 0) or 0)

    top_reasons: List[Tuple[str, int]] = sorted(reason_counts.items(), key=lambda item: -item[1])[:12]
    top_roles: List[Tuple[str, int]] = sorted(role_counts.items(), key=lambda item: -item[1])[:8]
    top_families: List[Tuple[str, int]] = sorted(alpha_family_counts.items(), key=lambda item: -item[1])[:8]

    td = _safe_text(summary.get("trade_date") or orders_doc.get("trade_date") or audit_doc.get("trade_date"))
    phase = _safe_text(summary.get("phase") or orders_doc.get("tactical_phase") or audit_doc.get("tactical_phase"))
    n_raw = _safe_int(summary.get("n_raw"), 0) if "n_raw" in summary else _safe_int(audit_doc.get("n_intents"), 0)
    discipline_summary = dict((summary.get("outer_intelligence") or {}) or {})

    summary_lines: List[str] = []
    if td or phase:
        summary_lines.append(f"盘中战术：交易日 {td or '-'}，阶段 {phase or '-'}。")
    if n_raw or intents or orders:
        summary_lines.append(f"原始意图 {max(n_raw, len(intents))} 条，仲裁后待发订单 {len(orders)} 笔（加仓 {add_count} / 减仓 {reduce_count}）。")
    if conflicts:
        summary_lines.append(f"同标的冲突 {len(conflicts)} 组，已按优先级和账户目标统一裁决。")
    if top_reasons:
        summary_lines.append("主要 reason_code：" + "，".join(f"{code}x{count}" for code, count in top_reasons[:3]) + "。")
    if top_roles:
        summary_lines.append("组合服务角色：" + "，".join(f"{role}x{count}" for role, count in top_roles[:3]) + "。")
    if intents:
        summary_lines.append(f"意图保留率 {kept_ratio:.0%}，减仓订单占比 {reduce_ratio:.0%}。")
    if discipline_summary:
        summary_lines.append(
            "外层纪律："
            f" posture={_safe_text(discipline_summary.get('discipline_posture')) or '-'}"
            f", suppressed={_safe_int(discipline_summary.get('suppressed_intents'))}"
            f", kept={_safe_int(discipline_summary.get('kept_intents'))}。"
        )

    conflict_rows: List[Dict[str, Any]] = []
    for row in conflicts[:24]:
        conflict_rows.append(
            {
                "symbol": _safe_text(row.get("symbol")),
                "winner_intent_id": _safe_text(row.get("winner_intent_id")),
                "n_suppressed": len(list(row.get("suppressed_intent_ids") or [])),
                "resolution": _safe_text(row.get("resolution")),
            }
        )

    block_reasons: List[str] = []
    for row in suppressed[:16]:
        symbol = _safe_text(row.get("symbol"))
        supp = row.get("suppressed") or []
        block_reasons.append(f"{symbol}: 压制 {len(supp)} 条意图")

    available = bool(summary or intents or orders or conflicts or audit_doc)
    return {
        "available": available,
        "artifact_paths": {
            "tactics_latest_dir": str(tactics_latest),
            "latest_intraday_tactical_audit_json": str(audit_latest) if audit_latest.exists() else "",
        },
        "trade_date": td,
        "tactical_phase": phase,
        "n_raw_intents": n_raw,
        "n_input_intents": len(intents),
        "n_arbitrated_orders": len(orders),
        "add_order_count": add_count,
        "reduce_order_count": reduce_count,
        "intent_keep_ratio": kept_ratio,
        "reduce_order_ratio": reduce_ratio,
        "n_symbol_conflicts": len(conflicts),
        "portfolio_service_role_counts": [{"role": key, "count": value} for key, value in top_roles],
        "intent_side_counts": [{"side": key, "count": value} for key, value in sorted(side_counts.items(), key=lambda item: -item[1])],
        "alpha_family_counts": [{"alpha_family": key, "count": value} for key, value in top_families],
        "reason_code_counts": [{"reason_code": key, "count": value} for key, value in top_reasons],
        "outer_intelligence_summary": discipline_summary,
        "conflict_summary": conflict_rows,
        "block_reasons": block_reasons,
        "summary_lines": summary_lines,
    }
