from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Tuple
from uuid import uuid4

import pandas as pd

from .config_utils import ensure_dir
from .execution_bridge_runner import execution_policy
from .runtime_protocol import artifact_identity
from .sql_store import (
    append_runtime_jsonl_record,
    ensure_schema,
    load_runtime_json_artifact,
    mirror_runtime_json_artifact,
    resolve_sqlite_path,
    sql_store_enabled,
    sqlite_connection,
    upsert_runtime_json_artifact,
)
from .trading_clock import clock_now, current_execution_window, is_trading_day, load_execution_windows, next_trading_day

RELEASE_SCHEMA_VERSION = 1


def _paths(config: Dict[str, Any]) -> Dict[str, Path]:
    paths = dict(config.get("paths", {}) or {})
    root = ensure_dir(Path(str(paths.get("trade_release_root", "") or "")).resolve())
    latest_root = ensure_dir(root / "latest")
    releases_root = ensure_dir(root / "releases")
    history_path = root / "release_history.jsonl"
    latest_pointer = root / "latest_release.json"
    return {
        "root": root,
        "latest_root": latest_root,
        "releases_root": releases_root,
        "history_path": history_path,
        "latest_pointer": latest_pointer,
    }


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    config = getattr(_load_json, "_active_config", None)
    if isinstance(config, dict) and sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if payload:
                    return payload
            except Exception:
                pass
    return {}


def _mirror_json_to_sql(config: Dict[str, Any], path: Path, payload: Dict[str, Any]) -> None:
    mirror_runtime_json_artifact(config, path, payload)


def _append_jsonl_to_sql(config: Dict[str, Any], path: Path, payload: Dict[str, Any], record_id: str = "") -> None:
    if not sql_store_enabled(config):
        return
    with sqlite_connection(resolve_sqlite_path(config)) as conn:
        ensure_schema(conn)
        append_runtime_jsonl_record(conn, path, payload, record_id=record_id)


def _copy_if_exists(src: Path, dst: Path) -> str:
    if not src.exists():
        return ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return str(dst)


def _copy_optional_artifact(summary: Dict[str, Any], artifact_key: str, release_dir: Path, filename: str) -> str:
    raw = str(dict(summary.get("artifacts", {}) or {}).get(artifact_key, "") or "").strip()
    if not raw:
        return ""
    src = Path(raw).resolve()
    return _copy_if_exists(src, release_dir / filename)


def _remove_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_paths(config: Dict[str, Any], summary_path: str = "", target_positions_path: str = "") -> Tuple[Path, Path, Path | None]:
    portfolio_root = Path(str(config.get("paths", {}).get("portfolio_output_root", "") or "")).resolve()
    summary = Path(summary_path).resolve() if str(summary_path).strip() else portfolio_root / "portfolio_recommendation.json"
    target = Path(target_positions_path).resolve() if str(target_positions_path).strip() else portfolio_root / "target_positions.csv"
    rebalance = portfolio_root / "rebalance_orders.csv"
    return summary, target, rebalance if rebalance.exists() else None


def _resolve_trade_date(config: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    trade_day_today = is_trading_day(config=config, target_date=now.date())
    execution_windows = load_execution_windows(config)
    allow_same_day = bool(trade_day_today.get("ok", False) and trade_day_today.get("is_trading_day", False))
    if execution_windows:
        current_time = now.timetz().replace(tzinfo=None)
        has_remaining_window_today = any(current_time <= window.end for window in execution_windows)
        allow_same_day = bool(allow_same_day and has_remaining_window_today)
    if allow_same_day:
        trade_date = now.date().isoformat()
        return {
            "ok": True,
            "trade_date": trade_date,
            "selection_reason": "same_day_with_remaining_execution_window",
        }
    next_day = next_trading_day(config=config, base_date=now.date(), include_today=False)
    return {
        "ok": bool(next_day.get("ok", False)),
        "trade_date": str(next_day.get("next_trading_day", "") or ""),
        "selection_reason": "next_trading_day",
        "calendar_path": str(next_day.get("calendar_path", "") or ""),
    }


def _forced_trade_date_info(config: Dict[str, Any], now: datetime, forced_trade_date: str) -> Dict[str, Any]:
    raw = str(forced_trade_date or "").strip()
    if not raw:
        return {}
    try:
        forced_date = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"非法 trade_date 覆盖值: {raw}") from exc
    if forced_date < now.date():
        raise ValueError(f"拒绝发布过期 release，trade_date={forced_date.isoformat()} 早于今天 {now.date().isoformat()}")
    trading_day = is_trading_day(config=config, target_date=forced_date)
    if not bool(trading_day.get("ok", False)) or not bool(trading_day.get("is_trading_day", False)):
        raise RuntimeError(f"指定 trade_date 不是交易日，无法发布 release: {forced_date.isoformat()}")
    return {
        "ok": True,
        "trade_date": forced_date.isoformat(),
        "selection_reason": "operator_forced_trade_date",
        "calendar_path": str(trading_day.get("calendar_path", "") or ""),
    }


def _release_constraints(config: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    portfolio_cfg = dict(config.get("portfolio_recommendation", {}) or {})
    control_cfg = dict(config.get("portfolio_control", {}) or {})
    broker_cfg = dict(config.get("broker", {}) or {})
    if not broker_cfg:
        bridge_cfg = dict(config.get("execution_bridge_runtime", {}) or {})
        broker_cfg = dict(bridge_cfg.get("broker", {}) or {})
    portfolio_limits = dict(summary.get("portfolio_limits", {}) or {})
    return {
        "single_name_cap": float(portfolio_limits.get("single_name_cap", portfolio_cfg.get("single_name_cap", 0.10)) or 0.10),
        "total_exposure_cap": float(portfolio_limits.get("total_exposure_cap", portfolio_cfg.get("total_exposure_cap", 1.0)) or 1.0),
        "max_names": int(portfolio_limits.get("max_names", portfolio_cfg.get("max_names", 20)) or 20),
        "max_daily_turnover_ratio": float(control_cfg.get("max_daily_turnover_ratio", 0.25) or 0.25),
        "drift_threshold": float(control_cfg.get("drift_threshold", 0.005) or 0.005),
        "cash_reserve_ratio": float(broker_cfg.get("cash_reserve_ratio", 0.02) or 0.02),
    }


def publish_portfolio_release(
    config: Dict[str, Any],
    source_mode: str,
    profile: str,
    summary_path: str = "",
    target_positions_path: str = "",
    note: str = "",
    forced_trade_date: str = "",
) -> Dict[str, Any]:
    _load_json._active_config = config
    now = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    src_summary, src_target, src_rebalance = _source_paths(config=config, summary_path=summary_path, target_positions_path=target_positions_path)
    if not src_summary.exists():
        raise FileNotFoundError(f"未找到持仓建议摘要文件: {src_summary}")
    if not src_target.exists():
        raise FileNotFoundError(f"未找到目标持仓文件: {src_target}")

    target_df = pd.read_csv(src_target)
    if target_df.empty:
        raise ValueError(f"目标持仓为空，拒绝发布 release: {src_target}")
    summary = _load_json(src_summary)
    release_cfg = dict(config.get("trade_release", {}) or {})
    if not bool(summary.get("simulation_ready", True)) and not bool(release_cfg.get("allow_not_ready_release", False)):
        reason = str(summary.get("research_deployment_reason", "") or "simulation_ready_false")
        raise RuntimeError(f"组合建议未通过研究/模拟门禁，拒绝发布 release: {reason}")

    trade_date_info = _forced_trade_date_info(config=config, now=now, forced_trade_date=forced_trade_date)
    if not trade_date_info:
        trade_date_info = _resolve_trade_date(config=config, now=now)
    if not bool(trade_date_info.get("ok", False)):
        raise RuntimeError("交易日历不可用，无法安全发布 portfolio release。")
    trade_date = str(trade_date_info.get("trade_date", "") or "")
    window = current_execution_window(config=config, now=now)
    execution_windows = load_execution_windows(config)
    valid_after_text = str(release_cfg.get("valid_after_time", "") or (execution_windows[0].start.strftime("%H:%M:%S") if execution_windows else "09:30:30"))
    expires_at_text = str(release_cfg.get("expires_at_time", "15:00:00") or "15:00:00")

    release_id = f"release_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    path_map = _paths(config)
    release_dir = ensure_dir(path_map["releases_root"] / release_id)
    target_copy = release_dir / "target_positions.csv"
    summary_copy = release_dir / "portfolio_recommendation.json"
    rebalance_copy = release_dir / "rebalance_orders.csv"
    manifest_path = release_dir / "release_manifest.json"

    _copy_if_exists(src_target, target_copy)
    _copy_if_exists(src_summary, summary_copy)
    if src_rebalance is not None:
        _copy_if_exists(src_rebalance, rebalance_copy)
    market_state_copy = _copy_optional_artifact(summary=summary, artifact_key="market_state_path", release_dir=release_dir, filename="latest_market_state.json")
    integrated_thesis_copy = _copy_optional_artifact(summary=summary, artifact_key="integrated_thesis_state_path", release_dir=release_dir, filename="integrated_thesis_state.json")
    tech_copy = _copy_optional_artifact(summary=summary, artifact_key="technical_confirmation_path", release_dir=release_dir, filename="technical_confirmation.csv")
    tech_summary_copy = _copy_optional_artifact(summary=summary, artifact_key="technical_confirmation_summary_path", release_dir=release_dir, filename="technical_confirmation_summary.json")
    posture_copy = _copy_optional_artifact(summary=summary, artifact_key="portfolio_posture_path", release_dir=release_dir, filename="latest_portfolio_posture.json")
    lifecycle_copy = _copy_optional_artifact(summary=summary, artifact_key="position_lifecycle_path", release_dir=release_dir, filename="latest_position_lifecycle.csv")
    lifecycle_daily_copy = _copy_optional_artifact(summary=summary, artifact_key="position_lifecycle_daily_path", release_dir=release_dir, filename="position_lifecycle_daily.csv")
    admission_copy = _copy_optional_artifact(summary=summary, artifact_key="admission_replacement_audit_path", release_dir=release_dir, filename="admission_replacement_audit.json")
    control_summary_copy = _copy_optional_artifact(summary=summary, artifact_key="portfolio_control_summary_path", release_dir=release_dir, filename="portfolio_control_summary.json")
    evidence_summary_copy = _copy_optional_artifact(summary=summary, artifact_key="evidence_audit_summary_path", release_dir=release_dir, filename="evidence_audit_summary.json")
    evidence_reviews_copy = _copy_optional_artifact(summary=summary, artifact_key="evidence_audit_reviews_path", release_dir=release_dir, filename="evidence_audit_reviews.json")
    evidence_sources_copy = _copy_optional_artifact(summary=summary, artifact_key="evidence_audit_sources_path", release_dir=release_dir, filename="evidence_audit_sources.json")

    release_doc = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "release_id": release_id,
        "status": "published",
        "generated_at": now.isoformat(timespec="seconds"),
        "source_mode": str(source_mode or "research_only"),
        "profile": str(profile or ""),
        "trade_date": trade_date,
        "valid_after": f"{trade_date}T{valid_after_text}+08:00",
        "expires_at": f"{trade_date}T{expires_at_text}+08:00",
        "selection_reason": str(trade_date_info.get("selection_reason", "") or ""),
        "note": str(note or ""),
        "target_count": int(len(target_df.index)),
        "strategy_name": str(summary.get("strategy_name", "") or ""),
        "strategy_key": str(summary.get("strategy_key", "") or ""),
        "formal_strategy_framework": str(summary.get("formal_strategy_framework", "integrated_event_industry_earnings_alpha") or "integrated_event_industry_earnings_alpha"),
        "primary_strategy_key": str(summary.get("primary_strategy_key", "") or ""),
        "run_id": str(summary.get("run_id", "") or ""),
        "simulation_ready": bool(summary.get("simulation_ready", True)),
        "execution_policy": execution_policy(config),
        "constraints": _release_constraints(config=config, summary=summary),
        "market_state": dict(summary.get("market_state", {}) or {}),
        "integrated_thesis_state": dict(summary.get("integrated_thesis_state", {}) or {}),
        "technical_confirmation": dict(summary.get("technical_confirmation", {}) or {}),
        "portfolio": dict(summary.get("portfolio", {}) or {}),
        "portfolio_posture": dict(summary.get("portfolio_posture", {}) or {}),
        "trade_discipline": dict(summary.get("trade_discipline", {}) or {}),
        "alpha_lifecycle": dict(summary.get("alpha_lifecycle", {}) or {}),
        "llm_operating_brain": dict(summary.get("llm_operating_brain", {}) or {}),
        "artifacts": {
            "manifest_path": str(manifest_path),
            "release_dir": str(release_dir),
            "target_positions_path": str(target_copy),
            "portfolio_summary_path": str(summary_copy),
            "rebalance_orders_path": str(rebalance_copy) if rebalance_copy.exists() else "",
            "market_state_path": str(market_state_copy),
            "integrated_thesis_state_path": str(integrated_thesis_copy),
            "technical_confirmation_path": str(tech_copy),
            "technical_confirmation_summary_path": str(tech_summary_copy),
            "portfolio_posture_path": str(posture_copy),
            "position_lifecycle_path": str(lifecycle_copy),
            "position_lifecycle_daily_path": str(lifecycle_daily_copy),
            "admission_replacement_audit_path": str(admission_copy),
            "portfolio_control_summary_path": str(control_summary_copy),
            "evidence_audit_summary_path": str(evidence_summary_copy),
            "evidence_audit_reviews_path": str(evidence_reviews_copy),
            "evidence_audit_sources_path": str(evidence_sources_copy),
            "source_target_positions_path": str(src_target),
            "source_portfolio_summary_path": str(src_summary),
        },
        "checksums": {
            "target_positions_sha256": _sha256_of_file(target_copy),
            "portfolio_summary_sha256": _sha256_of_file(summary_copy),
        },
        "research_snapshot": {
            "generated_at": str(summary.get("generated_at", "") or ""),
            "total_score": float(summary.get("total_score", 0.0) or 0.0),
            "sharpe": float(summary.get("sharpe", 0.0) or 0.0),
            "n_names": int(summary.get("n_names", len(target_df.index)) or len(target_df.index)),
            "active_window_at_publish": window.label if window else "",
        },
    }
    release_doc["artifact_identity"] = artifact_identity(
        run_id=str(summary.get("run_id", "") or ""),
        trade_date=trade_date,
        release_id=release_id,
        phase="release_manifest",
        producer="portfolio_release",
        generated_at=str(release_doc.get("generated_at", "") or ""),
        schema_version=str(RELEASE_SCHEMA_VERSION),
    )
    manifest_path.write_text(json.dumps(release_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    _mirror_json_to_sql(config, manifest_path, release_doc)

    latest_root = path_map["latest_root"]
    optional_latest_paths = [
        latest_root / "rebalance_orders.csv",
        latest_root / "latest_market_state.json",
        latest_root / "integrated_thesis_state.json",
        latest_root / "technical_confirmation.csv",
        latest_root / "technical_confirmation_summary.json",
        latest_root / "latest_portfolio_posture.json",
        latest_root / "latest_position_lifecycle.csv",
        latest_root / "position_lifecycle_daily.csv",
        latest_root / "admission_replacement_audit.json",
        latest_root / "portfolio_control_summary.json",
        latest_root / "evidence_audit_summary.json",
        latest_root / "evidence_audit_reviews.json",
        latest_root / "evidence_audit_sources.json",
    ]
    for stale_path in optional_latest_paths:
        _remove_if_exists(stale_path)
    _copy_if_exists(manifest_path, latest_root / "release_manifest.json")
    _copy_if_exists(target_copy, latest_root / "target_positions.csv")
    _copy_if_exists(summary_copy, latest_root / "portfolio_recommendation.json")
    if rebalance_copy.exists():
        _copy_if_exists(rebalance_copy, latest_root / "rebalance_orders.csv")
    if market_state_copy:
        _copy_if_exists(Path(market_state_copy), latest_root / "latest_market_state.json")
    if integrated_thesis_copy:
        _copy_if_exists(Path(integrated_thesis_copy), latest_root / "integrated_thesis_state.json")
    if tech_copy:
        _copy_if_exists(Path(tech_copy), latest_root / "technical_confirmation.csv")
    if tech_summary_copy:
        _copy_if_exists(Path(tech_summary_copy), latest_root / "technical_confirmation_summary.json")
    if posture_copy:
        _copy_if_exists(Path(posture_copy), latest_root / "latest_portfolio_posture.json")
    if lifecycle_copy:
        _copy_if_exists(Path(lifecycle_copy), latest_root / "latest_position_lifecycle.csv")
    if lifecycle_daily_copy:
        _copy_if_exists(Path(lifecycle_daily_copy), latest_root / "position_lifecycle_daily.csv")
    if admission_copy:
        _copy_if_exists(Path(admission_copy), latest_root / "admission_replacement_audit.json")
    if control_summary_copy:
        _copy_if_exists(Path(control_summary_copy), latest_root / "portfolio_control_summary.json")
    if evidence_summary_copy:
        _copy_if_exists(Path(evidence_summary_copy), latest_root / "evidence_audit_summary.json")
    if evidence_reviews_copy:
        _copy_if_exists(Path(evidence_reviews_copy), latest_root / "evidence_audit_reviews.json")
    if evidence_sources_copy:
        _copy_if_exists(Path(evidence_sources_copy), latest_root / "evidence_audit_sources.json")

    pointer_doc = {
        "release_id": release_id,
        "trade_date": trade_date,
        "status": "published",
        "generated_at": release_doc["generated_at"],
        "manifest_path": str(manifest_path),
        "target_positions_path": str(target_copy),
        "profile": str(profile or ""),
        "source_mode": str(source_mode or "research_only"),
        "artifact_identity": dict(release_doc.get("artifact_identity", {}) or {}),
    }
    path_map["latest_pointer"].write_text(json.dumps(pointer_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    _mirror_json_to_sql(config, path_map["latest_pointer"], pointer_doc)
    with path_map["history_path"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(pointer_doc, ensure_ascii=False) + "\n")
    _append_jsonl_to_sql(config, path_map["history_path"], pointer_doc, record_id=release_id)
    return release_doc


def load_latest_release(config: Dict[str, Any]) -> Dict[str, Any]:
    _load_json._active_config = config
    pointer_path = _paths(config)["latest_pointer"]
    if not pointer_path.exists():
        raise FileNotFoundError(f"未找到 latest release 指针: {pointer_path}")
    pointer = _load_json(pointer_path)
    manifest_path = Path(str(pointer.get("manifest_path", "") or "")).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"latest release manifest 不存在: {manifest_path}")
    return _load_json(manifest_path)


def load_release_by_id(config: Dict[str, Any], release_id: str) -> Dict[str, Any]:
    _load_json._active_config = config
    manifest_path = _paths(config)["releases_root"] / str(release_id).strip() / "release_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"未找到指定 release: {manifest_path}")
    return _load_json(manifest_path)


def record_release_execution(release_doc: Dict[str, Any], execution_record: Dict[str, Any]) -> Dict[str, Any]:
    config = getattr(record_release_execution, "_active_config", None)
    manifest_path = Path(str(release_doc.get("artifacts", {}).get("manifest_path", "") or "")).resolve()
    release_dir = manifest_path.parent
    history_path = release_dir / "execution_history.jsonl"
    latest_path = release_dir / "latest_execution.json"
    enriched_record = dict(execution_record)
    if not dict(enriched_record.get("artifact_identity", {}) or {}):
        release_identity = dict(release_doc.get("artifact_identity", {}) or {})
        enriched_record["artifact_identity"] = artifact_identity(
            run_id=str(release_identity.get("run_id", "") or release_doc.get("run_id", "") or ""),
            trade_date=str(release_identity.get("trade_date", "") or release_doc.get("trade_date", "") or ""),
            release_id=str(release_identity.get("release_id", "") or release_doc.get("release_id", "") or ""),
            phase="release_execution_record",
            producer="portfolio_release.record_execution",
            parent_lineage_token=str(release_identity.get("lineage_token", "") or ""),
        )
    latest_path.write_text(json.dumps(enriched_record, ensure_ascii=False, indent=2), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(enriched_record, ensure_ascii=False) + "\n")
    if isinstance(config, dict):
        _mirror_json_to_sql(config, latest_path, enriched_record)
        _append_jsonl_to_sql(
            config,
            history_path,
            enriched_record,
            record_id=str(enriched_record.get("timestamp", "") or enriched_record.get("generated_at", "") or uuid4().hex),
        )
    return {
        "history_path": str(history_path),
        "latest_execution_path": str(latest_path),
    }
