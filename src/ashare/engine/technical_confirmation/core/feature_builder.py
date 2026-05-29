from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

import pandas as pd


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_ts_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    code = "".join(ch for ch in text if ch.isdigit()).zfill(6)
    if not code:
        return ""
    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return f"{code}.SZ"
    if code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _code_from_any(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _load_price_frame(enriched_dir: Path, symbol: str) -> pd.DataFrame:
    code = _code_from_any(symbol)
    if not code:
        return pd.DataFrame()
    path = enriched_dir / f"{code}.csv"
    if not path.exists():
        return pd.DataFrame()
    cols = ["date", "close", "amount", "turnover_rate", "pct_chg"]
    try:
        df = pd.read_csv(path, usecols=lambda c: c in cols)
    except Exception:
        return pd.DataFrame()
    for col in cols:
        if col not in df.columns:
            df[col] = pd.NA
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    for col in ["close", "amount", "turnover_rate", "pct_chg"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


def _feature_row(price_df: pd.DataFrame, is_existing_position: bool) -> Dict[str, Any]:
    if price_df.empty:
        return {
            "close": 0.0,
            "ma10": 0.0,
            "ma20": 0.0,
            "ma60": 0.0,
            "ret_3": 0.0,
            "ret_5": 0.0,
            "ret_20": 0.0,
            "amount_ratio_20": 1.0,
            "turnover_ratio_20": 1.0,
            "price_vs_ma20": 0.0,
            "volatility_10": 0.0,
            "is_existing_position": bool(is_existing_position),
        }
    df = price_df.copy()
    for window in [10, 20, 60]:
        df[f"ma{window}"] = df["close"].rolling(window, min_periods=window).mean()
    df["amount_ma20"] = df["amount"].rolling(20, min_periods=20).mean()
    df["turnover_ma20"] = df["turnover_rate"].rolling(20, min_periods=20).mean()
    df["ret_3"] = df["close"] / df["close"].shift(3) - 1.0
    df["ret_5"] = df["close"] / df["close"].shift(5) - 1.0
    df["ret_20"] = df["close"] / df["close"].shift(20) - 1.0
    df["volatility_10"] = df["close"].pct_change().rolling(10, min_periods=5).std()
    row = df.iloc[-1]
    ma20 = _safe_float(row.get("ma20"), 0.0)
    return {
        "close": _safe_float(row.get("close"), 0.0),
        "ma10": _safe_float(row.get("ma10"), 0.0),
        "ma20": ma20,
        "ma60": _safe_float(row.get("ma60"), 0.0),
        "ret_3": _safe_float(row.get("ret_3"), 0.0),
        "ret_5": _safe_float(row.get("ret_5"), 0.0),
        "ret_20": _safe_float(row.get("ret_20"), 0.0),
        "amount_ratio_20": (_safe_float(row.get("amount"), 0.0) / _safe_float(row.get("amount_ma20"), 0.0)) if _safe_float(row.get("amount_ma20"), 0.0) > 0 else 1.0,
        "turnover_ratio_20": (_safe_float(row.get("turnover_rate"), 0.0) / _safe_float(row.get("turnover_ma20"), 0.0)) if _safe_float(row.get("turnover_ma20"), 0.0) > 0 else 1.0,
        "price_vs_ma20": (_safe_float(row.get("close"), 0.0) / ma20 - 1.0) if ma20 > 0 else 0.0,
        "volatility_10": _safe_float(row.get("volatility_10"), 0.0),
        "is_existing_position": bool(is_existing_position),
    }


def build_candidate_features(candidate_df: pd.DataFrame, prev_symbols: Iterable[str], enriched_dir: Path) -> pd.DataFrame:
    prev_keys: Set[str] = {
        _normalize_ts_code(item) for item in list(prev_symbols or []) if _normalize_ts_code(item)
    }
    rows: List[Dict[str, Any]] = []
    for _, row in candidate_df.iterrows():
        ts_code = _normalize_ts_code(row.get("ts_code") or row.get("code") or row.get("symbol"))
        if not ts_code:
            continue
        code = _code_from_any(ts_code)
        price_df = _load_price_frame(enriched_dir=enriched_dir, symbol=ts_code)
        features = _feature_row(price_df=price_df, is_existing_position=ts_code in prev_keys)
        date = str(price_df["date"].iloc[-1]) if not price_df.empty else str(row.get("date", "") or "")
        rows.append(
            {
                "date": date,
                "symbol": ts_code,
                "ts_code": ts_code,
                "code": code,
                **features,
            }
        )
    return pd.DataFrame(rows)
