from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pandas as pd


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _read_csv(path: Path, required: tuple[str, ...] = ()) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=list(required))
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=list(required))
    for col in required:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def _history_baselines(history_path: Path) -> Dict[str, float]:
    df = _read_csv(history_path)
    if df.empty:
        return {"total_amount_ratio_20": 1.0, "median_turnover_ratio_20": 1.0}
    df = df.sort_values("date").tail(20).copy()
    total_amount_mean = _safe_float(pd.to_numeric(df.get("total_amount"), errors="coerce").dropna().mean(), 0.0)
    turnover_mean = _safe_float(pd.to_numeric(df.get("median_turnover_rate"), errors="coerce").dropna().mean(), 0.0)
    return {
        "total_amount_mean_20": total_amount_mean,
        "median_turnover_mean_20": turnover_mean,
    }


def _load_snapshot(snapshot_path: Path) -> pd.DataFrame:
    required = ("date", "code", "ts_code", "close", "pre_close", "pct_chg", "amount", "turnover_rate", "total_mv", "circ_mv")
    snapshot = _read_csv(snapshot_path, required=required)
    if snapshot.empty:
        return snapshot
    for col in ["close", "pre_close", "pct_chg", "amount", "turnover_rate", "total_mv", "circ_mv"]:
        snapshot[col] = pd.to_numeric(snapshot[col], errors="coerce")
    snapshot["date"] = snapshot["date"].astype(str).str.slice(0, 10)
    snapshot["code"] = snapshot["code"].astype(str).str.strip()
    snapshot["ts_code"] = snapshot["ts_code"].astype(str).str.strip().str.upper()
    snapshot = snapshot.dropna(subset=["date", "code", "close"])
    return snapshot


def _load_hs300(hs300_path: Path) -> pd.DataFrame:
    df = _read_csv(hs300_path, required=("date", "close"))
    if df.empty:
        return df
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        return df
    for window in [20, 60]:
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=window).mean()
    for horizon in [1, 5, 20]:
        df[f"ret_{horizon}"] = df["close"] / df["close"].shift(horizon) - 1.0
    return df


def _top_bottom_bucket_returns(snapshot: pd.DataFrame) -> Tuple[float, float]:
    if snapshot.empty or "total_mv" not in snapshot.columns:
        return 0.0, 0.0
    df = snapshot.dropna(subset=["total_mv", "pct_chg"]).copy()
    if len(df.index) < 10:
        avg_ret = _safe_float(pd.to_numeric(df.get("pct_chg"), errors="coerce").mean(), 0.0) / 100.0
        return avg_ret, avg_ret
    df["mv_rank"] = df["total_mv"].rank(pct=True, method="average")
    large = df.loc[df["mv_rank"] >= 0.8]
    small = df.loc[df["mv_rank"] <= 0.2]
    large_ret = _safe_float(pd.to_numeric(large.get("pct_chg"), errors="coerce").mean(), 0.0) / 100.0
    small_ret = _safe_float(pd.to_numeric(small.get("pct_chg"), errors="coerce").mean(), 0.0) / 100.0
    return large_ret, small_ret


def _load_latest_signal_scores(signal_path: Path) -> Dict[str, float]:
    df = _read_csv(signal_path, required=("date", "mechanism_primary", "final_score", "signal_state"))
    if df.empty:
        return {}
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    latest_date = str(df["date"].max())
    latest = df.loc[df["date"] == latest_date].copy()
    if latest.empty:
        return {}
    latest = latest.loc[latest["signal_state"].astype(str).isin(["entry", "hold"])]
    if latest.empty:
        return {}
    latest["final_score"] = pd.to_numeric(latest["final_score"], errors="coerce")
    scores = latest.groupby("mechanism_primary")["final_score"].mean().to_dict()
    return {str(k): _safe_float(v) for k, v in scores.items()}


def _load_backtest_scores(attribution_path: Path) -> Dict[str, float]:
    if not attribution_path.exists():
        return {}
    try:
        payload = json.loads(attribution_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = list(payload.get("rows", []) or [])
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    if frame.empty or "mechanism_group" not in frame.columns:
        return {}
    frame["avg_forward_return"] = pd.to_numeric(frame.get("avg_forward_return"), errors="coerce").fillna(0.0)
    frame["candidate_count"] = pd.to_numeric(frame.get("candidate_count"), errors="coerce").fillna(0.0)
    grouped = {}
    for mechanism, g in frame.groupby("mechanism_group"):
        weighted = float((g["avg_forward_return"] * g["candidate_count"].clip(lower=1.0)).sum())
        denom = float(g["candidate_count"].clip(lower=1.0).sum()) or 1.0
        grouped[str(mechanism)] = weighted / denom
    return grouped


def build_market_feature_snapshot(config: Dict[str, Any], output_root: Path) -> Dict[str, Any]:
    market_cfg = dict(config.get("market_pipeline", {}) or {})
    snapshot_path = Path(str(market_cfg.get("price_snapshot_path", "") or "")).resolve()
    hs300_path = Path(str(market_cfg.get("hs300_path", "") or "")).resolve()
    history_path = output_root / "market_state_daily.csv"
    snapshot = _load_snapshot(snapshot_path)
    hs300 = _load_hs300(hs300_path)
    if snapshot.empty:
        return {
            "ok": False,
            "reason": f"price_snapshot_unavailable:{snapshot_path}",
            "date": "",
            "metrics": {},
            "mechanism_scores": {},
        }
    latest_date = str(snapshot["date"].astype(str).max())
    latest = snapshot.loc[snapshot["date"].astype(str) == latest_date].copy()
    if latest.empty:
        latest = snapshot.copy()
    latest["pct_chg_dec"] = pd.to_numeric(latest["pct_chg"], errors="coerce").fillna(0.0) / 100.0
    latest["amount"] = pd.to_numeric(latest["amount"], errors="coerce").fillna(0.0)
    latest["turnover_rate"] = pd.to_numeric(latest["turnover_rate"], errors="coerce").fillna(0.0)
    latest["total_mv"] = pd.to_numeric(latest["total_mv"], errors="coerce")
    total_amount = float(latest["amount"].sum())
    median_turnover = _safe_float(latest["turnover_rate"].median(), 0.0)
    advancers_ratio = float((latest["pct_chg_dec"] > 0).mean()) if len(latest.index) else 0.0
    large_drop_ratio = float((latest["pct_chg_dec"] <= -0.03).mean()) if len(latest.index) else 0.0
    strong_up_ratio = float((latest["pct_chg_dec"] >= 0.03).mean()) if len(latest.index) else 0.0
    limit_down_ratio = float((latest["pct_chg_dec"] <= -0.095).mean()) if len(latest.index) else 0.0
    avg_pct = _safe_float(latest["pct_chg_dec"].mean(), 0.0)
    median_pct = _safe_float(latest["pct_chg_dec"].median(), 0.0)
    active_share = 0.0
    if total_amount > 0 and len(latest.index) >= 10:
        active_share = float(latest.nlargest(max(1, int(len(latest.index) * 0.2)), "amount")["amount"].sum() / total_amount)
    large_cap_return, small_cap_return = _top_bottom_bucket_returns(latest)
    history = _history_baselines(history_path)
    total_amount_ratio_20 = 1.0
    total_amount_mean_20 = _safe_float(history.get("total_amount_mean_20"), 0.0)
    if total_amount_mean_20 > 0:
        total_amount_ratio_20 = total_amount / total_amount_mean_20
    median_turnover_ratio_20 = 1.0
    turnover_mean_20 = _safe_float(history.get("median_turnover_mean_20"), 0.0)
    if turnover_mean_20 > 0:
        median_turnover_ratio_20 = median_turnover / turnover_mean_20

    hs300_metrics = {
        "hs300_ret_1": 0.0,
        "hs300_ret_5": 0.0,
        "hs300_ret_20": 0.0,
        "hs300_above_ma20": False,
        "hs300_above_ma60": False,
        "hs300_close": 0.0,
        "hs300_ma20_gap": 0.0,
        "hs300_ma60_gap": 0.0,
    }
    if not hs300.empty:
        row = hs300.iloc[-1]
        close = _safe_float(row.get("close"), 0.0)
        ma20 = _safe_float(row.get("ma20"), 0.0)
        ma60 = _safe_float(row.get("ma60"), 0.0)
        hs300_metrics.update(
            {
                "hs300_close": close,
                "hs300_ret_1": _safe_float(row.get("ret_1"), 0.0),
                "hs300_ret_5": _safe_float(row.get("ret_5"), 0.0),
                "hs300_ret_20": _safe_float(row.get("ret_20"), 0.0),
                "hs300_above_ma20": bool(close > ma20) if ma20 > 0 else False,
                "hs300_above_ma60": bool(close > ma60) if ma60 > 0 else False,
                "hs300_ma20_gap": (close / ma20 - 1.0) if ma20 > 0 else 0.0,
                "hs300_ma60_gap": (close / ma60 - 1.0) if ma60 > 0 else 0.0,
            }
        )

    industry_root = Path(str(config.get("paths", {}).get("industry_router_output_root", "") or "")).resolve()
    signal_scores = _load_latest_signal_scores(industry_root / "latest_stock_signal.csv")
    backtest_scores = _load_backtest_scores(industry_root / "backtests" / "backtest_attribution_summary.json")
    mechanism_scores: Dict[str, float] = {}
    for mechanism in sorted(set(list(signal_scores.keys()) + list(backtest_scores.keys()))):
        mechanism_scores[mechanism] = round(signal_scores.get(mechanism, 0.0) * 0.7 + backtest_scores.get(mechanism, 0.0) * 6.0, 6)

    return {
        "ok": True,
        "reason": "ok",
        "date": latest_date,
        "metrics": {
            "avg_pct_chg": avg_pct,
            "median_pct_chg": median_pct,
            "advancers_ratio": advancers_ratio,
            "large_drop_ratio": large_drop_ratio,
            "strong_up_ratio": strong_up_ratio,
            "limit_down_ratio": limit_down_ratio,
            "total_amount": total_amount,
            "total_amount_ratio_20": total_amount_ratio_20,
            "median_turnover_rate": median_turnover,
            "median_turnover_ratio_20": median_turnover_ratio_20,
            "active_amount_share": active_share,
            "large_cap_return_1d": large_cap_return,
            "small_cap_return_1d": small_cap_return,
            "size_spread_1d": small_cap_return - large_cap_return,
            **hs300_metrics,
        },
        "mechanism_scores": mechanism_scores,
    }
