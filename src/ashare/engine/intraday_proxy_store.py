from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from .config_utils import ensure_dir
from .eastmoney_client import EastmoneyClient
from .portfolio_release import load_latest_release
from .research_fact_store import ensure_schema, resolve_research_fact_sqlite_path, sqlite_connection, upsert_rows
from .trading_clock import clock_now, market_stage
from .tushare_client import TushareClient


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _trade_clock_root(config: Dict[str, Any]) -> Path:
    raw = _text(dict(config.get("paths", {}) or {}).get("trade_clock_root"))
    default_root = _repo_root() / "data" / "trade_clock"
    return ensure_dir(Path(raw).resolve() if raw else default_root.resolve())


def _intraday_proxy_root(config: Dict[str, Any]) -> Path:
    cfg = dict(config.get("market_pipeline", {}) or {})
    raw = _text(cfg.get("intraday_proxy_root"))
    default_root = _trade_clock_root(config) / "intraday_proxy"
    return ensure_dir(Path(raw).resolve() if raw else default_root.resolve())


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _active_namespace(config: Dict[str, Any]) -> str:
    clock_path = _trade_clock_root(config) / "clock_state.json"
    payload = _load_json(clock_path)
    mode = _text(payload.get("account_mode") or dict(payload.get("gate", {}) or {}).get("account_mode")).lower()
    return mode if mode in {"precision", "simulation"} else "main"


def _oms_root(config: Dict[str, Any], namespace: str) -> Path:
    raw = _text(dict(config.get("paths", {}) or {}).get("oms_output_root"))
    base = Path(raw).resolve() if raw else (_repo_root() / "data" / "live_execution_bridge" / "oms_v1").resolve()
    return base if namespace == "main" else base / namespace


def _load_positions(config: Dict[str, Any], namespace: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    snapshot = _load_json(_oms_root(config, namespace) / "snapshots" / "latest_actual_portfolio_state.json")
    positions = list(snapshot.get("positions") or [])
    filtered: List[Dict[str, Any]] = []
    for row in positions:
        shares = _float(row.get("actual_shares") or row.get("shares") or row.get("volume"))
        market_value = _float(row.get("market_value") or row.get("amount"))
        if abs(shares) > 1e-9 or abs(market_value) > 1e-6:
            filtered.append(dict(row))
    snapshot["positions"] = filtered
    return snapshot, filtered


def _load_targets(config: Dict[str, Any]) -> List[str]:
    symbols: List[str] = []
    try:
        latest_release = load_latest_release(config)
    except Exception:
        latest_release = {}
    artifacts = dict(latest_release.get("artifacts", {}) or {})
    target_path = Path(_text(artifacts.get("target_positions_path"))).resolve() if _text(artifacts.get("target_positions_path")) else Path()
    if not target_path.exists():
        return symbols
    try:
        df = pd.read_csv(target_path)
    except Exception:
        return symbols
    for field in ("symbol", "ts_code", "code"):
        if field not in df.columns:
            continue
        for value in df[field].dropna().astype(str).tolist():
            text = value.strip().upper()
            if text and text not in symbols:
                symbols.append(text)
    return symbols


def _tracked_symbols(config: Dict[str, Any], namespace: str) -> List[str]:
    symbols: List[str] = []
    _, positions = _load_positions(config, namespace)
    for row in positions:
        for key in ("symbol", "ts_code", "code"):
            text = _text(row.get(key)).upper()
            if text and text not in symbols:
                symbols.append(text)
    for symbol in _load_targets(config):
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _normalize_quote_frame(frame: pd.DataFrame, captured_at: str, trade_date: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.columns = [str(col).strip().lower() for col in out.columns]
    if "ts_code" not in out.columns:
        return pd.DataFrame()
    out["ts_code"] = out["ts_code"].astype(str).str.strip().str.upper()
    out["symbol"] = out["ts_code"].map(lambda value: value.split(".", 1)[0] if "." in value else value.zfill(6))
    out["trade_date"] = trade_date
    out["snapshot_time"] = captured_at
    out["snapshot_id"] = out["trade_date"] + "::" + out["snapshot_time"] + "::" + out["ts_code"]
    rename_map = {"current": "price", "b1_p": "bid1", "a1_p": "ask1", "vol": "volume"}
    out = out.rename(columns=rename_map)
    for column in ["name", "price", "open", "high", "low", "pre_close", "bid1", "ask1", "volume", "amount"]:
        if column not in out.columns:
            out[column] = None
    out["source_name"] = "tushare.realtime_quote"
    out["source_class"] = "proxy_intraday_truth"
    out["raw_payload_path"] = ""
    return out[["snapshot_id", "trade_date", "snapshot_time", "symbol", "ts_code", "name", "price", "open", "high", "low", "pre_close", "bid1", "ask1", "volume", "amount", "source_name", "source_class", "raw_payload_path"]].copy()


def _normalize_list_frame(frame: pd.DataFrame, captured_at: str, trade_date: str, limit: int) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    out = frame.copy().head(max(int(limit or 0), 0))
    out.columns = [str(col).strip().lower() for col in out.columns]
    if "ts_code" not in out.columns:
        return pd.DataFrame()
    out["ts_code"] = out["ts_code"].astype(str).str.strip().str.upper()
    out["symbol"] = out["ts_code"].map(lambda value: value.split(".", 1)[0] if "." in value else value.zfill(6))
    out["trade_date"] = trade_date
    out["snapshot_time"] = captured_at
    out["row_id"] = out["trade_date"] + "::" + out["snapshot_time"] + "::" + out["ts_code"]
    rename_map = {"pct_chg": "pct_change", "swing": "amplitude"}
    out = out.rename(columns=rename_map)
    for column in ["name", "price", "pct_change", "amplitude", "volume_ratio", "turnover_rate", "total_mv", "circ_mv"]:
        if column not in out.columns:
            out[column] = None
    out["rank_bucket"] = "top_list"
    out["source_name"] = "tushare.realtime_list"
    out["source_class"] = "proxy_intraday_truth"
    out["raw_payload_path"] = ""
    return out[["row_id", "trade_date", "snapshot_time", "symbol", "ts_code", "name", "price", "pct_change", "amplitude", "volume_ratio", "turnover_rate", "total_mv", "circ_mv", "rank_bucket", "source_name", "source_class", "raw_payload_path"]].copy()


def _normalize_rt_min_frame(frame: pd.DataFrame, captured_at: str, trade_date: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.columns = [str(col).strip().lower() for col in out.columns]
    if "ts_code" not in out.columns:
        return pd.DataFrame()
    if "time" not in out.columns:
        return pd.DataFrame()
    out["ts_code"] = out["ts_code"].astype(str).str.strip().str.upper()
    out["symbol"] = out["ts_code"].map(lambda value: value.split(".", 1)[0] if "." in value else value.zfill(6))
    out["trade_date"] = trade_date
    out["snapshot_time"] = captured_at
    out["row_id"] = out["trade_date"] + "::" + out["snapshot_time"] + "::" + out["ts_code"] + "::" + out["time"].astype(str)
    for column in ["open", "close", "high", "low", "vol", "amount"]:
        if column not in out.columns:
            out[column] = None
    out["source_name"] = "tushare.rt_min"
    out["source_class"] = "official_intraday_bar"
    out["raw_payload_path"] = ""
    return out[["row_id", "trade_date", "snapshot_time", "symbol", "ts_code", "time", "open", "close", "high", "low", "vol", "amount", "source_name", "source_class", "raw_payload_path"]].copy()


def _load_rt_min_frame(
    *,
    config: Dict[str, Any],
    client: TushareClient,
    symbols: List[str],
    freq: str,
    trade_date: str,
) -> pd.DataFrame:
    market_cfg = dict(config.get("market_pipeline", {}) or {})
    provider = str(market_cfg.get("rt_min_provider", "eastmoney") or "eastmoney").strip().lower()
    if provider == "eastmoney":
        em_client = EastmoneyClient(dict(config.get("eastmoney", {}) or {}))
        frame = em_client.intraday_bars(ts_codes=symbols, freq=freq, trade_date=trade_date)
        if not frame.empty:
            return frame
        fallback_provider = str(market_cfg.get("rt_min_fallback_provider", "tushare") or "tushare").strip().lower()
        if fallback_provider != "tushare":
            return pd.DataFrame()
    return client.rt_min(ts_codes=symbols, freq=freq)


def _summarize_tick_frame(ts_code: str, frame: pd.DataFrame, captured_at: str, trade_date: str) -> Dict[str, Any]:
    if frame.empty:
        return {}
    out = frame.copy()
    out.columns = [str(col).strip().lower() for col in out.columns]
    price_col = "price" if "price" in out.columns else ""
    vol_col = "vol" if "vol" in out.columns else ("volume" if "volume" in out.columns else "")
    amount_col = "amount" if "amount" in out.columns else ""
    bs_col = "type" if "type" in out.columns else ("bs" if "bs" in out.columns else "")
    prices = pd.to_numeric(out[price_col], errors="coerce") if price_col else pd.Series(dtype="float64")
    vols = pd.to_numeric(out[vol_col], errors="coerce") if vol_col else pd.Series(dtype="float64")
    amounts = pd.to_numeric(out[amount_col], errors="coerce") if amount_col else (prices * vols if not prices.empty and not vols.empty else pd.Series(dtype="float64"))
    bs_series = out[bs_col].astype(str).str.lower() if bs_col else pd.Series([""] * len(out))
    buy_mask = bs_series.str.contains("buy|b")
    sell_mask = bs_series.str.contains("sell|s")
    neutral_mask = ~(buy_mask | sell_mask)
    symbol = ts_code.split(".", 1)[0] if "." in ts_code else ts_code
    return {
        "row_id": f"{trade_date}::{captured_at}::{ts_code}",
        "trade_date": trade_date,
        "snapshot_time": captured_at,
        "symbol": symbol,
        "ts_code": ts_code,
        "n_ticks": int(len(out.index)),
        "buy_amount": round(float(amounts.loc[buy_mask].fillna(0).sum()), 2),
        "sell_amount": round(float(amounts.loc[sell_mask].fillna(0).sum()), 2),
        "neutral_amount": round(float(amounts.loc[neutral_mask].fillna(0).sum()), 2),
        "latest_price": float(prices.dropna().iloc[-1]) if not prices.dropna().empty else 0.0,
        "source_name": "tushare.realtime_tick",
        "source_class": "proxy_intraday_truth",
        "raw_payload_path": "",
    }


def _order_health_counts(config: Dict[str, Any], namespace: str) -> Dict[str, int]:
    health = _load_json(_trade_clock_root(config) / "latest_account_health.json")
    order_health = dict(health.get("order_health", {}) or {})
    counts = {
        "pending_orders_count": int(order_health.get("open_count", 0) or 0),
        "unfinished_orders_count": int(order_health.get("open_count", 0) or 0),
    }
    return counts


def _build_account_truth_row(config: Dict[str, Any], trade_date: str, captured_at: str, namespace: str) -> Dict[str, Any]:
    health = _load_json(_trade_clock_root(config) / "latest_account_health.json")
    oms_state, positions = _load_positions(config, namespace)
    account_state = dict(health.get("account_state", {}) or {})
    oms_account = dict(oms_state.get("account", {}) or {})
    order_counts = _order_health_counts(config, namespace)
    sellable_positions_count = 0
    t1_locked_positions_count = 0
    for row in positions:
        shares = _float(row.get("actual_shares") or row.get("shares"))
        sellable = _float(row.get("sellable_shares") or row.get("available_shares"))
        if sellable > 0:
            sellable_positions_count += 1
        if shares > sellable:
            t1_locked_positions_count += 1
    nav = _float(account_state.get("nav") or account_state.get("total_asset") or oms_account.get("total_asset"))
    total_asset = _float(oms_account.get("total_asset") or nav)
    cash = _float(account_state.get("cash") or oms_account.get("cash"))
    available_cash = _float(account_state.get("available_cash") or oms_account.get("available_cash") or cash)
    frozen_cash = _float(oms_account.get("frozen_cash"))
    account_id = _text(account_state.get("account_id") or oms_account.get("account_id") or health.get("account_id"))
    return {
        "snapshot_id": f"{trade_date}::{captured_at}::{namespace}::{account_id or 'unknown'}",
        "trade_date": trade_date,
        "snapshot_time": captured_at,
        "account_id": account_id,
        "account_mode": namespace,
        "namespace": namespace,
        "nav": nav,
        "total_asset": total_asset,
        "cash": cash,
        "available_cash": available_cash,
        "frozen_cash": frozen_cash,
        "positions_count": int(len(positions)),
        "sellable_positions_count": int(sellable_positions_count),
        "pending_orders_count": int(order_counts.get("pending_orders_count", 0)),
        "unfinished_orders_count": int(order_counts.get("unfinished_orders_count", 0)),
        "t1_locked_positions_count": int(t1_locked_positions_count),
        "source_name": "broker_health+oms_runtime",
        "source_class": "derived_truth_bridge",
        "raw_payload_path": str((_oms_root(config, namespace) / "snapshots" / "latest_actual_portfolio_state.json").resolve()),
    }


def build_intraday_proxy_snapshot(config: Dict[str, Any], client: TushareClient, *, refresh_mode: str = "phase") -> Dict[str, Any]:
    market_cfg = dict(config.get("market_pipeline", {}) or {})
    namespace = _active_namespace(config)
    root = _intraday_proxy_root(config)
    latest_root = ensure_dir(root / "latest")
    now = clock_now(str(config.get("trade_clock", {}).get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
    captured_at = now.isoformat(timespec="seconds")
    trade_date = now.strftime("%Y-%m-%d")
    mode = str(refresh_mode or "phase").strip().lower() or "phase"
    current_stage = market_stage(now)
    symbols = _tracked_symbols(config, namespace)
    rapid_require_session = bool(market_cfg.get("rapid_refresh_require_trade_session", True))
    if mode == "rapid" and rapid_require_session and current_stage not in {"morning_session", "afternoon_session", "closing_auction"}:
        manifest = {
            "captured_at": captured_at,
            "trade_date": trade_date,
            "namespace": namespace,
            "tracked_symbols": symbols,
            "refresh_mode": mode,
            "market_stage": current_stage,
            "skipped": True,
            "skip_reason": "outside_trade_session",
        }
        (latest_root / "intraday_proxy_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    if mode == "rapid":
        rapid_symbol_limit = max(int(market_cfg.get("rapid_refresh_symbol_limit", 8) or 8), 1)
        symbols = symbols[:rapid_symbol_limit]

    quote_df = pd.DataFrame()
    if bool(market_cfg.get("realtime_quote_enabled", True)) and symbols:
        quote_df = _normalize_quote_frame(client.realtime_quote(ts_codes=symbols, src=str(market_cfg.get("realtime_quote_source", "sina") or "sina")), captured_at=captured_at, trade_date=trade_date)
    list_df = pd.DataFrame()
    list_enabled = bool(market_cfg.get("realtime_list_enabled", True))
    if mode == "rapid" and bool(market_cfg.get("rapid_refresh_skip_list", True)):
        list_enabled = False
    if list_enabled:
        list_df = _normalize_list_frame(client.realtime_list(src=str(market_cfg.get("realtime_list_source", "dc") or "dc")), captured_at=captured_at, trade_date=trade_date, limit=int(market_cfg.get("realtime_list_limit", 120) or 120))
    tick_rows: List[Dict[str, Any]] = []
    tick_enabled = bool(market_cfg.get("realtime_tick_enabled", True))
    if mode == "rapid" and bool(market_cfg.get("rapid_refresh_skip_tick", True)):
        tick_enabled = False
    if tick_enabled:
        tick_limit = max(int(market_cfg.get("realtime_tick_symbol_limit", 6) or 6), 0)
        for ts_code in symbols[:tick_limit]:
            row = _summarize_tick_frame(ts_code=ts_code, frame=client.realtime_tick(ts_code=ts_code, src=str(market_cfg.get("realtime_tick_source", "sina") or "sina")), captured_at=captured_at, trade_date=trade_date)
            if row:
                tick_rows.append(row)
    rt_min_df = pd.DataFrame()
    rt_min_enabled = bool(market_cfg.get("rt_min_enabled", False))
    if mode == "rapid" and bool(market_cfg.get("rapid_refresh_rt_min_enabled", rt_min_enabled)):
        rt_min_enabled = True
    if rt_min_enabled and symbols:
        rt_min_limit = max(int(market_cfg.get("rt_min_symbol_limit", 12) or 12), 1)
        rt_min_freq = str(market_cfg.get("rt_min_freq", "1MIN") or "1MIN")
        rt_min_df = _normalize_rt_min_frame(
            _load_rt_min_frame(
                config=config,
                client=client,
                symbols=symbols[:rt_min_limit],
                freq=rt_min_freq,
                trade_date=trade_date,
            ),
            captured_at=captured_at,
            trade_date=trade_date,
        )
        if not rt_min_df.empty:
            rt_min_source_name = "eastmoney.rt_min" if str(market_cfg.get("rt_min_provider", "eastmoney") or "eastmoney").strip().lower() == "eastmoney" else "tushare.rt_min"
            rt_min_df["source_name"] = rt_min_source_name
    account_truth = _build_account_truth_row(config=config, trade_date=trade_date, captured_at=captured_at, namespace=namespace)
    quote_path = latest_root / "intraday_quote_snapshot.csv"
    list_path = latest_root / "intraday_list_snapshot.csv"
    tick_path = latest_root / "intraday_tick_summary.csv"
    rt_min_path = latest_root / "intraday_rt_min_snapshot.csv"
    account_path = latest_root / "account_truth_snapshot.json"
    quote_df.to_csv(quote_path, index=False, encoding="utf-8-sig")
    list_df.to_csv(list_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(tick_rows).to_csv(tick_path, index=False, encoding="utf-8-sig")
    rt_min_df.to_csv(rt_min_path, index=False, encoding="utf-8-sig")
    account_path.write_text(json.dumps(account_truth, ensure_ascii=False, indent=2), encoding="utf-8")
    db_path = resolve_research_fact_sqlite_path(config)
    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        if not quote_df.empty:
            upsert_rows(conn, "intraday_proxy_quote_snapshot", quote_df.to_dict("records"), key_columns=("snapshot_id",))
        if not list_df.empty:
            upsert_rows(conn, "intraday_proxy_list_snapshot", list_df.to_dict("records"), key_columns=("row_id",))
        if tick_rows:
            upsert_rows(conn, "intraday_proxy_tick_summary", tick_rows, key_columns=("row_id",))
        upsert_rows(conn, "account_truth_snapshot", [account_truth], key_columns=("snapshot_id",))
    sources_used = ["account_truth"]
    if not quote_df.empty:
        sources_used.append("realtime_quote")
    if not list_df.empty:
        sources_used.append("realtime_list")
    if tick_rows:
        sources_used.append("realtime_tick")
    if not rt_min_df.empty:
        sources_used.append("rt_min")
    manifest = {
        "captured_at": captured_at,
        "trade_date": trade_date,
        "namespace": namespace,
        "tracked_symbols": symbols,
        "refresh_mode": mode,
        "market_stage": current_stage,
        "quote_rows": int(len(quote_df.index)),
        "list_rows": int(len(list_df.index)),
        "tick_rows": int(len(tick_rows)),
        "rt_min_rows": int(len(rt_min_df.index)),
        "account_truth": account_truth,
        "quote_path": str(quote_path),
        "list_path": str(list_path),
        "tick_path": str(tick_path),
        "rt_min_path": str(rt_min_path),
        "account_truth_path": str(account_path),
        "sources_used": sources_used,
        "freshness_class": "rapid" if mode == "rapid" else "phase_snapshot",
    }
    (latest_root / "intraday_proxy_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
