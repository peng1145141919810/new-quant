from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text == "NAN":
        return ""
    if "." in text:
        return text
    return text.zfill(6)


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except Exception:
        return default
    if pd.isna(out):
        return default
    return out


def _safe_div(numerator: Any, denominator: Any, default: float | None = None) -> float | None:
    num = _to_float(numerator, None)
    den = _to_float(denominator, None)
    if num is None or den in (None, 0.0):
        return default
    return float(num) / float(den)


def _paths(config: Dict[str, Any]) -> Dict[str, Path]:
    repo_root = Path(__file__).resolve().parents[4]
    local_data_root = repo_root / "data"
    external_data_root = Path(r"F:\quant_data\Ashare\data")
    live_root = Path(str(config.get("paths", {}).get("live_execution_root", local_data_root / "live_execution_bridge") or "")).resolve()
    technical_root = Path(str(config.get("paths", {}).get("technical_confirmation_root", local_data_root / "event_lake_v6" / "research" / "technical_confirmation") or "")).resolve()
    return {
        "live_snapshot": live_root / "daily_price_snapshot.csv",
        "live_snapshot_fallback": local_data_root / "live_execution_bridge" / "daily_price_snapshot.csv",
        "live_snapshot_external": external_data_root / "live_execution_bridge" / "daily_price_snapshot.csv",
        "intraday_proxy_root": local_data_root / "trade_clock" / "intraday_proxy" / "latest",
        "technical_latest": technical_root / "latest_technical_confirmation.csv",
        "technical_latest_external": external_data_root / "event_lake_v6" / "research" / "technical_confirmation" / "latest_technical_confirmation.csv",
        "affordable_root": local_data_root / "affordable_feeds" / "latest",
    }


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _read_csv(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _series_from(frame: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series([default] * len(frame.index), index=frame.index)


def _mask_from(frame: pd.DataFrame, series: Any) -> pd.Series:
    if isinstance(series, pd.Series):
        return series.reindex(frame.index, fill_value=False).astype(bool)
    return pd.Series([bool(series)] * len(frame.index), index=frame.index)


def _live_snapshot_frame(config: Dict[str, Any]) -> pd.DataFrame:
    paths = _paths(config)
    explicit_path = Path(
        str(config.get("paths", {}).get("live_price_snapshot_path", "") or "")
    ).resolve() if str(config.get("paths", {}).get("live_price_snapshot_path", "") or "").strip() else None
    path = _first_existing(
        explicit_path,
        paths["live_snapshot"],
        paths["live_snapshot_fallback"],
        paths["live_snapshot_external"],
    )
    frame = _read_csv(path)
    if frame.empty:
        return frame
    out = frame.copy()
    if "ts_code" not in out.columns and "code" in out.columns:
        out["ts_code"] = out["code"].map(_normalize_symbol)
    out["ts_code"] = _series_from(out, "ts_code").map(_normalize_symbol)
    if "code" not in out.columns and "ts_code" in out.columns:
        out["code"] = out["ts_code"].astype(str).str.split(".").str[0]
    for col in ("price", "close", "pre_close", "pct_chg", "amount", "turnover_rate", "open", "high", "low", "vwap", "volume"):
        if col not in out.columns:
            out[col] = pd.NA
    if "price" not in out.columns or out["price"].isna().all():
        out["price"] = out["close"]
    return out


def _technical_frame(config: Dict[str, Any]) -> pd.DataFrame:
    paths = _paths(config)
    path = _first_existing(paths["technical_latest"], paths["technical_latest_external"])
    frame = _read_csv(path)
    if frame.empty:
        return frame
    out = frame.copy()
    if "ts_code" not in out.columns and "symbol" in out.columns:
        out["ts_code"] = out["symbol"].map(_normalize_symbol)
    out["ts_code"] = _series_from(out, "ts_code").map(_normalize_symbol)
    return out


def _stock_basic_frame(config: Dict[str, Any]) -> pd.DataFrame:
    affordable_root = Path(
        str(config.get("paths", {}).get("affordable_snapshot_root", _paths(config)["affordable_root"]) or "")
    ).resolve()
    path = affordable_root / "stock_basic.csv"
    frame = _read_csv(path)
    if frame.empty:
        return frame
    out = frame.copy()
    out["ts_code"] = _series_from(out, "ts_code").map(_normalize_symbol)
    return out


def _event_window_frame(config: Dict[str, Any], trade_date: str) -> pd.DataFrame:
    affordable_root = Path(
        str(config.get("paths", {}).get("affordable_snapshot_root", _paths(config)["affordable_root"]) or "")
    ).resolve()
    trade_dt = None
    if str(trade_date or "").strip():
        try:
            trade_dt = datetime.strptime(str(trade_date or "")[:10], "%Y-%m-%d")
        except Exception:
            trade_dt = None
    rows: list[dict[str, Any]] = []
    for dataset_name in ("forecast", "express"):
        frame = _read_csv(affordable_root / f"{dataset_name}.csv")
        if frame.empty or "ts_code" not in frame.columns or "ann_date" not in frame.columns:
            continue
        current = frame.copy()
        current["ts_code"] = current["ts_code"].map(_normalize_symbol)
        current["ann_date"] = current["ann_date"].astype(str).str.slice(0, 8)
        if trade_dt is not None:
            current["days_from_trade_date"] = current["ann_date"].map(
                lambda x: (
                    abs((datetime.strptime(str(x), "%Y%m%d") - trade_dt).days)
                    if str(x or "").strip() and str(x or "").isdigit()
                    else 9999
                )
            )
            current = current.loc[current["days_from_trade_date"] <= 2].copy()
        if current.empty:
            continue
        current["event_dataset"] = dataset_name
        rows.extend(current[["ts_code", "ann_date", "event_dataset"]].to_dict("records"))
    return pd.DataFrame(rows)


def _limit_frame(config: Dict[str, Any], trade_date: str) -> pd.DataFrame:
    affordable_root = Path(
        str(config.get("paths", {}).get("affordable_snapshot_root", _paths(config)["affordable_root"]) or "")
    ).resolve()
    path = affordable_root / "stk_limit.csv"
    frame = _read_csv(path)
    if frame.empty:
        return frame
    out = frame.copy()
    out["ts_code"] = _series_from(out, "ts_code").map(_normalize_symbol)
    if trade_date:
        target = str(trade_date or "").replace("-", "")
        filtered = out.loc[_series_from(out, "trade_date").astype(str).eq(target)].copy()
        if not filtered.empty:
            return filtered
    if "trade_date" in out.columns:
        return out.sort_values(["trade_date"]).drop_duplicates(subset=["ts_code"], keep="last")
    return out.drop_duplicates(subset=["ts_code"], keep="last")


def _intraday_proxy_frames(config: Dict[str, Any]) -> Dict[str, Any]:
    root = Path(
        str(
            dict(config.get("market_pipeline", {}) or {}).get(
                "intraday_proxy_root",
                _paths(config)["intraday_proxy_root"],
            )
            or _paths(config)["intraday_proxy_root"]
        )
    ).resolve() / "latest"
    quote = _read_csv(root / "intraday_quote_snapshot.csv")
    if not quote.empty:
        if "ts_code" in quote.columns:
            quote["ts_code"] = quote["ts_code"].map(_normalize_symbol)
        elif "symbol" in quote.columns:
            quote["ts_code"] = quote["symbol"].map(_normalize_symbol)
        quote = quote.drop_duplicates(subset=["ts_code"], keep="last")
    top_list = _read_csv(root / "intraday_list_snapshot.csv")
    if not top_list.empty:
        if "ts_code" in top_list.columns:
            top_list["ts_code"] = top_list["ts_code"].map(_normalize_symbol)
        elif "symbol" in top_list.columns:
            top_list["ts_code"] = top_list["symbol"].map(_normalize_symbol)
        top_list = top_list.drop_duplicates(subset=["ts_code"], keep="first")
    tick = _read_csv(root / "intraday_tick_summary.csv")
    if not tick.empty:
        if "ts_code" in tick.columns:
            tick["ts_code"] = tick["ts_code"].map(_normalize_symbol)
        elif "symbol" in tick.columns:
            tick["ts_code"] = tick["symbol"].map(_normalize_symbol)
        tick = tick.drop_duplicates(subset=["ts_code"], keep="last")
    manifest_path = root / "intraday_proxy_manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    return {"quote": quote, "top_list": top_list, "tick": tick, "manifest": manifest}


def build_intraday_feature_frame(
    *,
    config: Dict[str, Any],
    trade_date: str,
    target_frame: pd.DataFrame,
    actual_positions_frame: pd.DataFrame,
    symbol_state_frame: pd.DataFrame,
    market_state: Dict[str, Any],
) -> pd.DataFrame:
    base = symbol_state_frame.copy() if symbol_state_frame is not None else pd.DataFrame()
    if base.empty:
        return pd.DataFrame()
    base["stock_code"] = base.get("stock_code", "").map(_normalize_symbol)
    base["ts_code"] = base["stock_code"]

    target = target_frame.copy() if target_frame is not None else pd.DataFrame()
    if not target.empty:
        if "ts_code" not in target.columns and "symbol" in target.columns:
            target["ts_code"] = target["symbol"].map(_normalize_symbol)
        elif "ts_code" in target.columns:
            target["ts_code"] = target["ts_code"].map(_normalize_symbol)
        target = target.drop_duplicates(subset=["ts_code"], keep="last")

    actual = actual_positions_frame.copy() if actual_positions_frame is not None else pd.DataFrame()
    if "ts_code" in actual.columns:
        actual["ts_code"] = actual["ts_code"].map(_normalize_symbol)
    elif "symbol" in actual.columns:
        actual["ts_code"] = actual["symbol"].map(_normalize_symbol)
    elif "stock_code" in actual.columns:
        actual["ts_code"] = actual["stock_code"].map(_normalize_symbol)
    else:
        actual["ts_code"] = ""
    if not actual.empty:
        actual = actual.drop_duplicates(subset=["ts_code"], keep="last")

    live = _live_snapshot_frame(config)
    tech = _technical_frame(config)
    stock_basic = _stock_basic_frame(config)
    event_window = _event_window_frame(config, trade_date)
    limit_frame = _limit_frame(config, trade_date)
    proxy_frames = _intraday_proxy_frames(config)
    proxy_quote = proxy_frames["quote"]
    proxy_top_list = proxy_frames["top_list"]
    proxy_tick = proxy_frames["tick"]
    proxy_manifest = dict(proxy_frames.get("manifest") or {})

    merged = base.merge(
        target.add_prefix("target_"),
        how="left",
        left_on="ts_code",
        right_on="target_ts_code",
    )
    merged = merged.merge(
        actual.add_prefix("actual_"),
        how="left",
        left_on="ts_code",
        right_on="actual_ts_code",
    )
    merged = merged.merge(
        live.add_prefix("live_"),
        how="left",
        left_on="ts_code",
        right_on="live_ts_code",
    )
    if not proxy_quote.empty:
        merged = merged.merge(
            proxy_quote.add_prefix("proxyq_"),
            how="left",
            left_on="ts_code",
            right_on="proxyq_ts_code",
        )
    merged = merged.merge(
        tech.add_prefix("tech_"),
        how="left",
        left_on="ts_code",
        right_on="tech_ts_code",
    )
    merged = merged.merge(
        stock_basic.add_prefix("basic_"),
        how="left",
        left_on="ts_code",
        right_on="basic_ts_code",
    )
    if not event_window.empty:
        event_bucket = event_window.groupby("ts_code")["event_dataset"].agg(lambda s: ",".join(sorted({str(x) for x in s if str(x)}))).reset_index()
        merged = merged.merge(event_bucket.add_prefix("event_"), how="left", left_on="ts_code", right_on="event_ts_code")
    if not limit_frame.empty:
        merged = merged.merge(limit_frame.add_prefix("limit_"), how="left", left_on="ts_code", right_on="limit_ts_code")
    if not proxy_top_list.empty:
        merged = merged.merge(proxy_top_list.add_prefix("proxyl_"), how="left", left_on="ts_code", right_on="proxyl_ts_code")
    if not proxy_tick.empty:
        merged = merged.merge(proxy_tick.add_prefix("proxyt_"), how="left", left_on="ts_code", right_on="proxyt_ts_code")

    merged["industry_effective"] = (
        merged.get("target_industry", pd.Series(dtype=object)).fillna("")
        .astype(str)
        .where(lambda s: s.str.strip() != "", merged.get("basic_industry", pd.Series(dtype=object)).fillna("").astype(str))
    )

    merged["snapshot_trade_date"] = merged.get("proxyq_trade_date", pd.Series(dtype=object)).fillna(
        merged.get("live_date", pd.Series(dtype=object))
    ).astype(str).str.slice(0, 10)
    merged["snapshot_stale"] = merged["snapshot_trade_date"].astype(str).ne(str(trade_date or "")[:10])
    merged["last_price"] = merged.get("proxyq_price", pd.Series(dtype=float)).fillna(
        merged.get("live_price", pd.Series(dtype=float)).fillna(merged.get("live_close", pd.Series(dtype=float)))
    )
    merged["prev_close"] = merged.get("live_pre_close", pd.Series(dtype=float)).fillna(merged.get("target_pre_close", pd.Series(dtype=float)))
    merged["open_price"] = merged.get("proxyq_open", pd.Series(dtype=float)).fillna(merged.get("live_open", pd.Series(dtype=float)))
    merged["day_high"] = merged.get("proxyq_high", pd.Series(dtype=float)).fillna(merged.get("live_high", pd.Series(dtype=float)))
    merged["day_low"] = merged.get("proxyq_low", pd.Series(dtype=float)).fillna(merged.get("live_low", pd.Series(dtype=float)))
    merged["vwap_price"] = merged.get("live_vwap", pd.Series(dtype=float))
    merged["current_amount"] = merged.get("proxyq_amount", pd.Series(dtype=float)).fillna(
        merged.get("live_amount", pd.Series(dtype=float)).fillna(merged.get("target_amount", pd.Series(dtype=float)))
    )
    merged["current_turnover_rate"] = merged.get("live_turnover_rate", pd.Series(dtype=float)).fillna(merged.get("target_turnover_rate", pd.Series(dtype=float)))
    merged["amount_mean_5"] = merged.get("target_amount_mean_5", pd.Series(dtype=float))
    merged["amount_mean_20"] = merged.get("target_amount_mean_20", pd.Series(dtype=float))
    merged["turnover_mean_5"] = merged.get("target_turnover_mean_5", pd.Series(dtype=float))
    merged["turnover_mean_20"] = merged.get("target_turnover_mean_20", pd.Series(dtype=float))
    merged["available_shares"] = merged.get("actual_available_shares", pd.Series(dtype=float)).fillna(0.0)
    merged["actual_shares"] = merged.get("actual_actual_shares", pd.Series(dtype=float)).fillna(0.0)
    merged["has_old_base_position"] = merged["available_shares"].fillna(0.0).gt(0)
    merged["proxy_spread_pct"] = (
        pd.to_numeric(merged.get("proxyq_ask1", pd.Series(dtype=float)), errors="coerce")
        - pd.to_numeric(merged.get("proxyq_bid1", pd.Series(dtype=float)), errors="coerce")
    ) / pd.to_numeric(merged["last_price"], errors="coerce")
    merged["proxy_spread_pct"] = pd.to_numeric(merged["proxy_spread_pct"], errors="coerce").fillna(0.0)
    merged["proxy_quote_available"] = pd.to_numeric(merged.get("proxyq_price", pd.Series(dtype=float)), errors="coerce").notna()
    merged["proxy_top_list_hit"] = _series_from(merged, "proxyl_ts_code").astype(str).str.strip().ne("")
    merged["proxy_list_pct_change"] = pd.to_numeric(merged.get("proxyl_pct_change", pd.Series(dtype=float)), errors="coerce") / 100.0
    merged["proxy_list_turnover_rate"] = pd.to_numeric(merged.get("proxyl_turnover_rate", pd.Series(dtype=float)), errors="coerce") / 100.0
    merged["proxy_buy_amount"] = pd.to_numeric(merged.get("proxyt_buy_amount", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    merged["proxy_sell_amount"] = pd.to_numeric(merged.get("proxyt_sell_amount", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    merged["proxy_tick_amount"] = merged["proxy_buy_amount"] + merged["proxy_sell_amount"] + pd.to_numeric(merged.get("proxyt_neutral_amount", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    merged["proxy_tick_imbalance"] = (
        merged["proxy_buy_amount"] - merged["proxy_sell_amount"]
    ) / merged["proxy_tick_amount"].where(merged["proxy_tick_amount"].abs().gt(1e-9), pd.NA)
    merged["proxy_tick_imbalance"] = pd.to_numeric(merged["proxy_tick_imbalance"], errors="coerce").fillna(0.0)
    merged["proxy_tick_count"] = pd.to_numeric(merged.get("proxyt_n_ticks", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    merged["proxy_market_heat_score"] = (
        merged["proxy_top_list_hit"].astype(float) * 0.45
        + pd.to_numeric(merged["proxy_list_pct_change"], errors="coerce").fillna(0.0).clip(lower=0.0, upper=0.12) * 2.0
        + pd.to_numeric(merged["proxy_tick_imbalance"], errors="coerce").fillna(0.0).clip(lower=0.0, upper=0.6) * 0.5
    ).clip(lower=0.0, upper=1.0)

    merged["last_price_vs_prev_close"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["prev_close"], errors="coerce") - 1.0
    )
    if "live_pct_chg" in merged.columns:
        live_pct = pd.to_numeric(merged["live_pct_chg"], errors="coerce") / 100.0
        merged["last_price_vs_prev_close"] = merged["last_price_vs_prev_close"].fillna(live_pct)
    merged["last_price_vs_open"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["open_price"], errors="coerce") - 1.0
    )
    merged["last_price_vs_vwap"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["vwap_price"], errors="coerce") - 1.0
    )
    merged["intraday_return_from_low"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["day_low"], errors="coerce") - 1.0
    )
    merged["intraday_return_from_high"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["day_high"], errors="coerce") - 1.0
    )
    merged["opening_gap_pct"] = (
        pd.to_numeric(merged["open_price"], errors="coerce") / pd.to_numeric(merged["prev_close"], errors="coerce") - 1.0
    )
    merged["opening_range_breakout_up"] = (
        pd.to_numeric(merged["last_price"], errors="coerce").gt(pd.to_numeric(merged["day_high"], errors="coerce") * 0.995)
    )
    merged["opening_range_breakout_down"] = (
        pd.to_numeric(merged["last_price"], errors="coerce").lt(pd.to_numeric(merged["day_low"], errors="coerce") * 1.005)
    )
    merged["morning_high_fail_flag"] = (
        pd.to_numeric(merged["intraday_return_from_high"], errors="coerce").lt(-0.015)
        & pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").gt(0.0)
    )
    merged["intraday_reversal_up_flag"] = (
        pd.to_numeric(merged["intraday_return_from_low"], errors="coerce").gt(0.01)
        & pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").gt(-0.01)
    )
    merged["intraday_reversal_down_flag"] = (
        pd.to_numeric(merged["intraday_return_from_high"], errors="coerce").lt(-0.01)
        & pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").lt(0.01)
    )

    merged["micro_trend_slope_short"] = pd.to_numeric(merged["last_price_vs_open"], errors="coerce").fillna(pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce"))
    merged["micro_trend_slope_medium"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce")
    merged["vwap_reclaim_flag"] = pd.to_numeric(merged["last_price_vs_vwap"], errors="coerce").gt(0.002)
    merged["vwap_break_flag"] = pd.to_numeric(merged["last_price_vs_vwap"], errors="coerce").lt(-0.002)
    merged["intraday_amplitude_pct"] = (
        pd.to_numeric(merged["day_high"], errors="coerce") / pd.to_numeric(merged["day_low"], errors="coerce") - 1.0
    ).fillna(pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").abs())
    merged["distance_from_vwap_pct"] = pd.to_numeric(merged["last_price_vs_vwap"], errors="coerce").abs()
    merged["distance_from_day_high_pct"] = (
        pd.to_numeric(merged["day_high"], errors="coerce") / pd.to_numeric(merged["last_price"], errors="coerce") - 1.0
    )
    merged["distance_from_day_low_pct"] = (
        pd.to_numeric(merged["last_price"], errors="coerce") / pd.to_numeric(merged["day_low"], errors="coerce") - 1.0
    )

    merged["relative_strength_vs_index"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce") - float(market_state.get("hs300_ret_1", 0.0) or 0.0)
    merged["industry_daily_move"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce")
    merged["industry_median_move"] = merged.groupby("industry_effective")["industry_daily_move"].transform("median")
    merged["relative_strength_vs_industry"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce") - pd.to_numeric(merged["industry_median_move"], errors="coerce").fillna(0.0)
    merged["relative_strength_rank_intraday"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").rank(pct=True).fillna(0.5)

    merged["intraday_amount_ratio"] = merged.apply(lambda row: _safe_div(row.get("current_amount"), row.get("amount_mean_20"), _to_float(row.get("tech_amount_ratio_20"), 1.0)), axis=1)
    merged["intraday_volume_ratio"] = merged.apply(lambda row: _safe_div(row.get("current_turnover_rate"), row.get("turnover_mean_20"), _to_float(row.get("tech_turnover_ratio_20"), 1.0)), axis=1)
    merged["turnover_acceleration"] = merged.apply(lambda row: _safe_div(row.get("current_turnover_rate"), row.get("turnover_mean_5"), _to_float(row.get("intraday_volume_ratio"), 1.0)), axis=1)
    merged["amount_acceleration"] = merged.apply(lambda row: _safe_div(row.get("current_amount"), row.get("amount_mean_5"), _to_float(row.get("intraday_amount_ratio"), 1.0)), axis=1)
    merged["price_up_amount_up_flag"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").gt(0) & pd.to_numeric(merged["intraday_amount_ratio"], errors="coerce").gt(1.0)
    merged["price_up_amount_down_flag"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").gt(0) & pd.to_numeric(merged["intraday_amount_ratio"], errors="coerce").le(1.0)
    merged["price_down_amount_up_flag"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").lt(0) & pd.to_numeric(merged["intraday_amount_ratio"], errors="coerce").gt(1.0)
    merged["relative_liquidity_vs_history"] = (
        pd.to_numeric(merged["intraday_amount_ratio"], errors="coerce").fillna(1.0)
        + pd.to_numeric(merged["intraday_volume_ratio"], errors="coerce").fillna(1.0)
    ) / 2.0
    merged["industry_amount_median"] = merged.groupby("industry_effective")["current_amount"].transform("median")
    merged["relative_amount_vs_industry"] = merged.apply(lambda row: _safe_div(row.get("current_amount"), row.get("industry_amount_median"), 1.0), axis=1)
    merged["volume_confirmation_flag"] = (
        pd.to_numeric(merged["intraday_amount_ratio"], errors="coerce").ge(1.0)
        | pd.to_numeric(merged["intraday_volume_ratio"], errors="coerce").ge(1.0)
        | pd.to_numeric(merged["relative_liquidity_vs_history"], errors="coerce").ge(1.05)
        | merged["proxy_top_list_hit"]
        | pd.to_numeric(merged["proxy_tick_imbalance"], errors="coerce").ge(0.10)
    )

    merged["limit_edge_flag"] = (
        (pd.to_numeric(merged.get("last_price", pd.Series(dtype=float)), errors="coerce") >= pd.to_numeric(merged.get("limit_up_limit", pd.Series(dtype=float)), errors="coerce") * 0.995)
        | (pd.to_numeric(merged.get("last_price", pd.Series(dtype=float)), errors="coerce") <= pd.to_numeric(merged.get("limit_down_limit", pd.Series(dtype=float)), errors="coerce") * 1.005)
    ).fillna(False)
    merged["major_event_window_flag"] = merged.get("event_event_dataset", pd.Series(dtype=object)).fillna("").astype(str).str.strip().ne("")
    merged["abnormal_volatility_proxy_flag"] = pd.to_numeric(merged["last_price_vs_prev_close"], errors="coerce").abs().ge(0.085) | merged["limit_edge_flag"]
    merged["message_veto_flag"] = (
        pd.to_numeric(merged.get("target_is_suspended", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0)
        | pd.to_numeric(merged.get("target_is_st", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0)
        | merged["major_event_window_flag"]
        | merged["abnormal_volatility_proxy_flag"]
    )
    merged["message_veto_reason"] = ""
    suspended_mask = _mask_from(merged, pd.to_numeric(_series_from(merged, "target_is_suspended", 0.0), errors="coerce").fillna(0).gt(0))
    st_mask = _mask_from(merged, pd.to_numeric(_series_from(merged, "target_is_st", 0.0), errors="coerce").fillna(0).gt(0))
    major_event_mask = _mask_from(merged, merged["major_event_window_flag"])
    abnormal_volatility_mask = _mask_from(merged, merged["abnormal_volatility_proxy_flag"])
    merged.loc[suspended_mask, "message_veto_reason"] += "suspended;"
    merged.loc[st_mask, "message_veto_reason"] += "st_flag;"
    merged.loc[major_event_mask, "message_veto_reason"] += "major_event_window;"
    merged.loc[abnormal_volatility_mask, "message_veto_reason"] += "abnormal_volatility_proxy;"
    merged["message_veto_reason"] = merged["message_veto_reason"].astype(str).str.strip(";")

    merged["low_liquidity_flag"] = (
        pd.to_numeric(merged["current_amount"], errors="coerce").fillna(0.0).le(0.0)
        | pd.to_numeric(merged["current_turnover_rate"], errors="coerce").fillna(0.0).le(0.0)
        | (
            merged["proxy_quote_available"]
            & pd.to_numeric(merged["proxy_spread_pct"], errors="coerce").fillna(0.0).ge(0.018)
        )
    )
    merged["feature_quality_tier"] = "full_intraday_snapshot"
    merged.loc[pd.to_numeric(merged["open_price"], errors="coerce").isna() | pd.to_numeric(merged["day_high"], errors="coerce").isna() | pd.to_numeric(merged["day_low"], errors="coerce").isna(), "feature_quality_tier"] = "snapshot_degraded"
    merged.loc[pd.to_numeric(merged["last_price"], errors="coerce").isna(), "feature_quality_tier"] = "no_live_snapshot"
    proxy_trade_date = str(proxy_manifest.get("trade_date", "") or "")[:10]
    if proxy_trade_date and proxy_trade_date != str(trade_date or "")[:10]:
        merged.loc[:, "feature_quality_tier"] = "proxy_stale"
    return merged
