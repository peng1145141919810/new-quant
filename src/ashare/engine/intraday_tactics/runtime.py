from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from ..config_utils import load_config
from ..sql_store import mirror_runtime_dataframe, mirror_runtime_json_artifact, mirror_runtime_jsonl_records
from ..trading_clock import clock_now
from .audit import build_latest_audit_summary, write_tactical_audit_jsonl
from .context_loader import load_tactical_context, tactics_root
from .intent_schema import IntradayActionIntent, IntradayTacticalRunSummary
from .policy import load_tactical_policy
from .priority_arbiter import arbitrate
from .trigger_engine import run_triggers


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        pd.DataFrame().to_csv(path, index=False, encoding="utf-8-sig")
        return
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _apply_policy_caps(winners: List[IntradayActionIntent], policy, nav: float) -> List[IntradayActionIntent]:
    if nav <= 0:
        return []
    out: List[IntradayActionIntent] = []
    for it in winners:
        max_notional = nav * (policy.max_symbol_add_ratio if it.side == "BUY" else policy.max_symbol_reduce_ratio)
        px = max(it.delta_notional_cap / max(it.delta_shares, 1), 1e-6)
        max_sh = int(max_notional / max(px, 1e-6) // 100 * 100)
        ds = min(it.delta_shares, max_sh) if max_sh > 0 else it.delta_shares
        if ds <= 0:
            continue
        out.append(replace(it, delta_shares=int(ds)))
    return out


def run_intraday_tactics_pipeline(
    config_path: Path,
    *,
    tactical_phase: str = "manual",
    execute: bool = True,
    gate_only: bool = False,
    trade_date: str = "",
) -> Dict[str, Any]:
    config = load_config(config_path)
    now_dt = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    td = str(trade_date or "").strip() or str(now_dt.date())
    ctx = load_tactical_context(config, trade_date=td)
    policy = load_tactical_policy(config)
    root = tactics_root(config)
    latest = root / "latest"
    latest.mkdir(parents=True, exist_ok=True)

    raw = run_triggers(ctx=ctx, policy=policy, tactical_phase=tactical_phase, now=now_dt.replace(tzinfo=None))
    # outer_intelligence 盘中仲裁已移除：触发意图直接进入机械冲突仲裁。
    intelligence_summary = {"enabled": False, "applied": False, "reason": "outer_intelligence_removed"}
    winners, conflicts, supp = arbitrate(raw, ctx=ctx)
    nav = float(dict(ctx.get("clock_account_snapshot", {}) or {}).get("nav", 0) or 0)
    winners = _apply_policy_caps(winners, policy, nav)

    intent_rows = [x.to_dict() for x in raw]
    arb_rows = [x.to_dict() for x in winners]
    conflict_rows = [c.to_dict() for c in conflicts]

    intents_doc = {"trade_date": td, "phase": tactical_phase, "intents": intent_rows}
    _atomic_write_json(latest / "intraday_action_intents.json", intents_doc)
    _write_csv(latest / "intraday_action_intents.csv", intent_rows)
    summary_doc = {
        "ok": True,
        "trade_date": td,
        "phase": tactical_phase,
        "n_raw": len(raw),
        "n_arbitrated": len(winners),
        "outer_intelligence": intelligence_summary,
    }
    _atomic_write_json(latest / "intraday_tactical_summary.json", summary_doc)
    conflicts_doc = {"trade_date": td, "conflicts": conflict_rows, "suppressed": supp}
    _atomic_write_json(latest / "intraday_tactical_conflicts.json", conflicts_doc)
    mirror_runtime_json_artifact(config, latest / "intraday_action_intents.json", intents_doc)
    mirror_runtime_json_artifact(config, latest / "intraday_tactical_summary.json", summary_doc)
    mirror_runtime_json_artifact(config, latest / "intraday_tactical_conflicts.json", conflicts_doc)
    intents_df = pd.DataFrame(intent_rows)
    if not intents_df.empty:
        ik = ["intent_id"] if "intent_id" in intents_df.columns else None
        mirror_runtime_dataframe(config, latest / "intraday_action_intents.csv", intents_df, key_cols=ik)

    orders: List[Dict[str, Any]] = []
    for it in winners:
        px = max(it.delta_notional_cap / max(it.delta_shares, 1), 0.01)
        orders.append(
            {
                "symbol": it.symbol,
                "side": it.side,
                "delta_shares": int(it.delta_shares),
                "ref_price": round(px, 4),
                "intent_id": it.intent_id,
                "intent_class": it.intent_class,
                "reason_code": it.reason_code,
                "rule_id": it.rule_id,
                "tactical_phase": tactical_phase,
            }
        )
    tactical_orders_payload = {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "trade_date": td,
        "tactical_phase": tactical_phase,
        "intent_source": "intraday_tactics",
        "execution_mode": str(config.get("execution_policy", {}).get("account_mode", "simulation")),
        "namespace": str(config.get("execution_policy", {}).get("namespace", "main")),
        "orders": orders,
    }
    _atomic_write_json(latest / "intraday_tactical_orders.json", tactical_orders_payload)
    mirror_runtime_json_artifact(config, latest / "intraday_tactical_orders.json", tactical_orders_payload)

    audit_root = Path(str(config.get("paths", {}).get("data_root", Path(__file__).resolve().parents[3] / "data"))).resolve() / "audit_v1"
    reason_counts: Dict[str, int] = {}
    for it in winners:
        reason_counts[it.reason_code] = int(reason_counts.get(it.reason_code, 0) or 0) + 1
    audit_json_path = build_latest_audit_summary(
        trade_date=td,
        tactical_phase=tactical_phase,
        n_intents=len(raw),
        n_orders=len(orders),
        reason_counts=reason_counts,
        audit_root=audit_root,
    )
    mirror_runtime_json_artifact(config, audit_json_path, json.loads(audit_json_path.read_text(encoding="utf-8")))
    tac_audit_rows = [
        {"event": "tactical_run", "phase": tactical_phase, "n_orders": len(orders), "ts": tactical_orders_payload["generated_at"]}
    ]
    audit_jsonl_path = audit_root / "latest" / "intraday_tactical_audit.jsonl"
    write_tactical_audit_jsonl(audit_jsonl_path, tac_audit_rows)
    mirror_runtime_jsonl_records(config, audit_jsonl_path, tac_audit_rows)

    orders_path = str((latest / "intraday_tactical_orders.json").resolve())

    exec_payload: Dict[str, Any] = {
        "ok": True,
        "trade_date": td,
        "tactical_phase": tactical_phase,
        "artifact_root": str(root),
        "intraday_tactical_orders_path": orders_path,
        "n_orders": len(orders),
    }

    if execute and not gate_only and orders:
        from ..execution_manager import run_execution_only

        exec_result = run_execution_only(
            config_path=config_path,
            release_id=str(ctx.get("release_id", "") or ""),
            ignore_window=False,
            gate_only=False,
            trigger_label=str(tactical_phase),
            trigger_source="intraday_tactics",
            intent_source="intraday_tactical",
            intraday_tactical_orders_path=orders_path,
        )
        exec_payload["execution"] = exec_result
    elif execute and not orders:
        exec_payload["execution"] = {"status": "skipped", "reason": "no_tactical_orders"}
    if isinstance(exec_payload.get("execution"), dict) and exec_payload["execution"].get("ok") is False:
        exec_payload["ok"] = False

    return exec_payload
