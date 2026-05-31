from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _bootstrap_repo() -> None:
    script_path = Path(__file__).resolve()
    package_root = script_path.parents[1] / "src/ashare" / "src/ashare"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))


_bootstrap_repo()

from engine.config_builder import build_runtime_config
from engine.industry_router import build_industry_router_artifacts
from engine.integrated_thesis import build_integrated_thesis_artifacts


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10]


def _event_store_path(config: Dict[str, Any]) -> Path | None:
    local_root_raw = str(config.get("paths", {}).get("event_store_root", "") or "").strip()
    candidates: List[Path] = []
    if local_root_raw:
        candidates.append(Path(local_root_raw).resolve() / "event_store.jsonl")
    candidates.append(Path(r"F:\quant_data\Ashare\data\event_lake\curated\event_store.jsonl"))
    for path in candidates:
        if path.exists():
            return path
    return None


def _load_sample_structured_events(config: Dict[str, Any], limit: int = 16) -> List[Dict[str, Any]]:
    path = _event_store_path(config)
    if path is None:
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            event_type = _text(payload.get("event_type")).lower()
            if event_type not in {"contract_award", "major_contract", "price_change", "investment", "business_development", "operational_change"}:
                continue
            event_id = _text(payload.get("event_id"))
            if event_id and event_id in seen:
                continue
            seen.add(event_id)
            out.append(payload)
            if len(out) >= limit:
                break
    return out


def _override_probe_paths(config: Dict[str, Any]) -> Path:
    probe_root = (Path.cwd() / "tmp" / "integrated_thesis_sql_join_probe").resolve()
    (probe_root / "industry_router").mkdir(parents=True, exist_ok=True)
    (probe_root / "integrated_thesis").mkdir(parents=True, exist_ok=True)
    config.setdefault("industry_router", {})
    config["industry_router"]["output_root"] = str(probe_root / "industry_router")
    config["industry_router"].setdefault("source_fetch", {})
    config["industry_router"]["source_fetch"]["enabled"] = False
    config["industry_router"]["enable_backtest"] = False
    config.setdefault("integrated_thesis", {})
    config["integrated_thesis"]["output_root"] = str(probe_root / "integrated_thesis")
    config.setdefault("paths", {})
    config["paths"]["industry_router_output_root"] = str(probe_root / "industry_router")
    return probe_root


def _table_counts(sqlite_path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        return {
            "event_fact_company_actions": conn.execute("SELECT COUNT(*) FROM event_fact_company_actions").fetchone()[0],
            "event_fact_contract_orders": conn.execute("SELECT COUNT(*) FROM event_fact_contract_orders").fetchone()[0],
            "event_fact_supply_chain_signals": conn.execute("SELECT COUNT(*) FROM event_fact_supply_chain_signals").fetchone()[0],
            "industry_factor_price_inventory_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_price_inventory_daily").fetchone()[0],
            "industry_factor_operation_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_operation_daily").fetchone()[0],
            "industry_factor_customs_summary_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_customs_summary_daily").fetchone()[0],
        }
    finally:
        conn.close()


def main() -> int:
    config = build_runtime_config()
    probe_root = _override_probe_paths(config)
    sqlite_path = Path(str(config.get("paths", {}).get("research_fact_sqlite_path", "") or "")).resolve()
    structured_events = _load_sample_structured_events(config, limit=16)

    router_result = build_industry_router_artifacts(config=config, structured_events=structured_events)
    market_state_payload = {
        "date": _normalize_date(datetime.now().strftime("%Y-%m-%d")),
        "risk_budget_multiplier": 0.85,
        "new_position_policy": "allow",
    }
    thesis_result = build_integrated_thesis_artifacts(
        config=config,
        structured_events=structured_events,
        industry_router_payload=dict(router_result.get("context_payload", {}) or {}),
        market_state_payload=market_state_payload,
    )
    payload = dict(thesis_result.get("payload", {}) or {})
    top_candidates = list(payload.get("top_candidates", []) or [])[:5]
    sample_rows = [
        {
            "symbol": _text(item.get("symbol")),
            "score": item.get("integrated_thesis_score"),
            "state": _text(item.get("integrated_thesis_state")),
            "event_fact_id": _text(item.get("primary_event_fact_id")),
            "gate_stage": _text(item.get("thesis_gate_stage")),
            "reject_reason": _text(item.get("thesis_reject_reason")),
            "reason_chain": list(item.get("thesis_reason_chain", []) or [])[:8],
        }
        for item in top_candidates
    ]

    summary = {
        "probe_root": str(probe_root),
        "research_fact_sqlite_path": str(sqlite_path),
        "research_fact_table_counts": _table_counts(sqlite_path) if sqlite_path.exists() else {},
        "sample_event_count": len(structured_events),
        "industry_router_status": _text(router_result.get("status")),
        "industry_router_signal_rows": int(dict(router_result.get("summary", {}) or {}).get("signal_rows", 0)),
        "integrated_thesis_status": _text(thesis_result.get("status")),
        "integrated_thesis_symbol_count": int(dict(payload.get("summary", {}) or {}).get("n_symbols", 0)),
        "integrated_thesis_accepted_count": int(dict(payload.get("summary", {}) or {}).get("n_accepted", 0)),
        "sample_candidates": sample_rows,
        "artifacts": {
            "router_summary_path": _text(router_result.get("summary_path")),
            "thesis_state_path": _text(thesis_result.get("latest_path")),
            "thesis_candidates_path": _text(thesis_result.get("candidates_path")),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
