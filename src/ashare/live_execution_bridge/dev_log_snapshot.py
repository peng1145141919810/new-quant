# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List


SNAPSHOT_START = "<!-- LIVE_PORTFOLIO_SNAPSHOT_START -->"
SNAPSHOT_END = "<!-- LIVE_PORTFOLIO_SNAPSHOT_END -->"


def _mask_account_id(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return text
    return f"{text[:4]}...{text[-4:]}"


def _float_text(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except Exception:
        return "0.0000"


def _position_weight_rows(positions: List[Dict[str, Any]], nav: float, top_n: int) -> List[str]:
    safe_nav = max(float(nav or 0.0), 1e-9)
    ranked = []
    for row in positions:
        shares = float(row.get("shares", 0) or 0)
        last_price = float(row.get("last_price", 0) or 0)
        weight = (shares * last_price) / safe_nav if last_price > 0 else 0.0
        ranked.append((weight, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    lines: List[str] = []
    for weight, row in ranked[:top_n]:
        lines.append(
            f"- `{row.get('symbol', '')}`: weight={weight:.4f}, shares={int(row.get('shares', 0) or 0)}, price={float(row.get('last_price', 0) or 0):.4f}"
        )
    return lines or ["- 暂无持仓快照"]


def _build_snapshot_block(payload: Dict[str, Any]) -> str:
    holdings = list(payload.get("top_holdings", []) or [])
    holding_lines = "\n".join(holdings or ["- 暂无持仓快照"])
    return (
        "## Latest Live Portfolio Snapshot\n"
        f"{SNAPSHOT_START}\n"
        f"- Updated at: `{payload.get('updated_at', '')}`\n"
        f"- Source report: `{payload.get('source_report', '')}`\n"
        f"- Account: `{payload.get('account_id_masked', '')}`\n"
        f"- NAV: `{payload.get('nav', '')}`\n"
        f"- Cash: `{payload.get('cash', '')}`\n"
        f"- Positions: `{payload.get('n_positions', 0)}`\n"
        f"- Target names: `{payload.get('n_target_positions', 0)}`\n"
        f"- Orders/Fills: `{payload.get('n_orders', 0)}` / `{payload.get('n_fills', 0)}`\n"
        f"- Turnover raw/final: `{payload.get('raw_turnover_ratio', '')}` / `{payload.get('final_turnover_ratio', '')}`\n"
        f"- Drift skipped: `{payload.get('n_drift_skipped_symbols', 0)}`\n"
        f"- Turnover adjustments: `{payload.get('n_turnover_adjustments', 0)}`\n"
        f"- Execution status summary: `success={payload.get('n_success', 0)} partial={payload.get('n_partial', 0)} failed={payload.get('n_failed', 0)} skipped={payload.get('n_skipped', 0)}`\n"
        "- Top holdings:\n"
        f"{holding_lines}\n"
        f"{SNAPSHOT_END}\n"
    )


def update_codex_dev_log_portfolio_snapshot(
    dev_log_path: str | Path,
    execution_report: Dict[str, Any],
    after_state: Dict[str, Any],
    control_summary: Dict[str, Any],
    execution_feedback: Dict[str, Any],
    top_holdings: int = 8,
) -> bool:
    path = Path(dev_log_path)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    positions = list(after_state.get("positions", []) or [])
    nav = float(after_state.get("nav", execution_report.get("after_nav", 0.0)) or 0.0)
    feedback_summary = dict(execution_feedback.get("summary", {}) or {})
    payload = {
        "updated_at": str(execution_report.get("timestamp", "")),
        "source_report": str(execution_report.get("execution_report_path", "") or ""),
        "account_id_masked": _mask_account_id(str(after_state.get("account_id", "") or "")),
        "nav": _float_text(nav),
        "cash": _float_text(after_state.get("cash", execution_report.get("after_cash", 0.0))),
        "n_positions": len(positions),
        "n_target_positions": int(execution_report.get("n_target_positions", 0) or 0),
        "n_orders": int(execution_report.get("n_orders", 0) or 0),
        "n_fills": int(execution_report.get("n_fills", 0) or 0),
        "raw_turnover_ratio": _float_text(control_summary.get("raw_turnover_ratio", 0.0)),
        "final_turnover_ratio": _float_text(control_summary.get("final_turnover_ratio", 0.0)),
        "n_drift_skipped_symbols": int(control_summary.get("n_drift_skipped_symbols", 0) or 0),
        "n_turnover_adjustments": int(control_summary.get("n_turnover_adjustments", 0) or 0),
        "n_success": int(feedback_summary.get("n_success", 0) or 0),
        "n_partial": int(feedback_summary.get("n_partial", 0) or 0),
        "n_failed": int(feedback_summary.get("n_failed", 0) or 0),
        "n_skipped": int(feedback_summary.get("n_skipped", 0) or 0),
        "top_holdings": _position_weight_rows(positions=positions, nav=nav, top_n=max(3, int(top_holdings))),
    }
    block = _build_snapshot_block(payload=payload)
    if SNAPSHOT_START in text and SNAPSHOT_END in text:
        pattern = re.compile(
            r"## Latest Live Portfolio Snapshot\s*" + re.escape(SNAPSHOT_START) + r".*?" + re.escape(SNAPSHOT_END) + r"\s*",
            flags=re.DOTALL,
        )
        new_text = pattern.sub(lambda _: block, text, count=1)
    else:
        marker = "## Session Start Checklist"
        if marker in text:
            new_text = text.replace(marker, block + "\n" + marker, 1)
        else:
            new_text = text + "\n\n" + block
    path.write_text(new_text, encoding="utf-8")
    return True
