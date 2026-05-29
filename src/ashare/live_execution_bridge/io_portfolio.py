from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

from .models import TargetPosition
from .utils import normalize_symbol


SYMBOL_COLUMNS = ["symbol", "ts_code", "code", "stock_code", "ticker"]
WEIGHT_COLUMNS = ["target_weight", "weight", "portfolio_weight", "target_pct", "pct"]
SCORE_COLUMNS = ["score", "pred_score", "signal", "rank_score"]
PRICE_COLUMNS = ["price", "close", "last_price", "last", "adj_close", "open"]


def _coerce_price(value: Any) -> float:
    try:
        price = float(value)
    except Exception:
        return 0.0
    return price if price > 0 else 0.0


def _coerce_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    ts = pd.to_datetime(text, errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def _row_value(row: pd.Series, key: str, raw_payload: Dict[str, Any]) -> Any:
    value = row.get(key)
    if value is not None:
        text = str(value).strip()
        if text and text.lower() != "nan":
            return value
    return raw_payload.get(key)


def _parse_raw_payload(row: pd.Series) -> Dict[str, Any]:
    raw_text = row.get("raw", row.get("metadata", row.get("context", "")))
    if raw_text is None:
        return {}
    text = str(raw_text).strip()
    if not text or text.lower() == "nan":
        return {}
    try:
        payload = ast.literal_eval(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _pick_portfolio_row_price(row: pd.Series) -> Tuple[float, pd.Timestamp | None]:
    raw_payload = _parse_raw_payload(row)
    last_price = _coerce_price(_row_value(row, "last_price", raw_payload) or _row_value(row, "last", raw_payload))
    last_date = _coerce_timestamp(_row_value(row, "last_price_date", raw_payload) or _row_value(row, "last_date", raw_payload))
    snapshot_price = _coerce_price(_row_value(row, "price", raw_payload))
    snapshot_date = _coerce_timestamp(_row_value(row, "price_date", raw_payload))
    close_price = _coerce_price(_row_value(row, "close", raw_payload))
    close_date = _coerce_timestamp(_row_value(row, "date", raw_payload))
    tech_close = _coerce_price(_row_value(row, "tech_close", raw_payload))
    tech_date = _coerce_timestamp(_row_value(row, "tech_date", raw_payload))

    execution_candidates = [
        (last_date, last_price),
        (snapshot_date, snapshot_price),
    ]
    execution_candidates = [(ts, price) for ts, price in execution_candidates if ts is not None and price > 0]
    if execution_candidates:
        latest_ts = max(ts for ts, _ in execution_candidates)
        latest_prices = [price for ts, price in execution_candidates if ts == latest_ts]
        if latest_prices:
            return float(latest_prices[0]), latest_ts

    if snapshot_price > 0:
        return snapshot_price, snapshot_date
    if last_price > 0:
        return last_price, last_date

    research_candidates = [
        (close_date, close_price),
        (tech_date, tech_close),
    ]
    research_candidates = [(ts, price) for ts, price in research_candidates if ts is not None and price > 0]
    if research_candidates:
        latest_ts = max(ts for ts, _ in research_candidates)
        latest_prices = [price for ts, price in research_candidates if ts == latest_ts]
        if latest_prices:
            return float(latest_prices[0]), latest_ts

    if close_price > 0:
        return close_price, close_date
    if tech_close > 0:
        return tech_close, tech_date
    return 0.0, None


def discover_latest_portfolio_file(config: Dict[str, Any]) -> Path:
    """发现最新的持仓建议文件。

    Args:
        config: 运行配置。

    Returns:
        Path: 识别到的最新持仓建议文件路径。
    """
    explicit = str(config.get("explicit_portfolio_path", "")).strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise FileNotFoundError(f"显式指定的持仓文件不存在: {p}")

    portfolio_root = Path(config["portfolio_root"])
    candidates = list(portfolio_root.rglob("latest_portfolio_v1.csv"))
    if not candidates:
        raise FileNotFoundError(f"未在目录中找到 latest_portfolio_v1.csv: {portfolio_root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _find_column(frame: pd.DataFrame, candidates: List[str]) -> str | None:
    """寻找匹配列名。

    Args:
        frame: 数据表。
        candidates: 候选列名。

    Returns:
        str | None: 命中的列名或空。
    """
    lower_map = {str(col).strip().lower(): col for col in frame.columns}
    for name in candidates:
        if name in lower_map:
            return str(lower_map[name])
    return None


def _normalize_weight_series(series: pd.Series, limit: float) -> pd.Series:
    """规范化权重列。

    Args:
        series: 原始权重序列。
        limit: 权重总和上限。

    Returns:
        pd.Series: 规范化后的权重序列。
    """
    clean = pd.to_numeric(series, errors="coerce").fillna(0.0)
    if clean.max() > 1.5:
        clean = clean / 100.0
    clean = clean.clip(lower=0.0)
    total = float(clean.sum())
    if total <= 0:
        return clean
    if total > limit:
        clean = clean * (limit / total)
    return clean


def load_target_positions(config: Dict[str, Any]) -> Tuple[Path, pd.DataFrame, List[TargetPosition]]:
    """加载目标仓位。

    Args:
        config: 运行配置。

    Returns:
        Tuple[Path, pd.DataFrame, List[TargetPosition]]: 文件路径、原始表和目标仓位列表。
    """
    portfolio_path = discover_latest_portfolio_file(config)
    frame = pd.read_csv(portfolio_path)
    if frame.empty:
        raise ValueError(f"持仓建议文件为空: {portfolio_path}")

    symbol_col = _find_column(frame, SYMBOL_COLUMNS)
    weight_col = _find_column(frame, WEIGHT_COLUMNS)
    score_col = _find_column(frame, SCORE_COLUMNS)
    if symbol_col is None:
        raise ValueError(f"未找到证券代码列，现有列: {list(frame.columns)}")

    frame = frame.copy()
    frame["__symbol"] = frame[symbol_col].map(normalize_symbol)
    frame = frame[frame["__symbol"].astype(str).str.len() > 0]

    portfolio_cfg = config.get("portfolio", {})
    limit = float(portfolio_cfg.get("weight_sum_limit", 0.98))
    max_names = int(portfolio_cfg.get("max_names", 20))
    fallback_equal = bool(portfolio_cfg.get("fallback_equal_weight", True))

    if weight_col is not None:
        frame["__weight"] = _normalize_weight_series(frame[weight_col], limit)
    elif fallback_equal:
        frame["__weight"] = 1.0 / max(len(frame), 1)
        frame["__weight"] = _normalize_weight_series(frame["__weight"], limit)
    else:
        raise ValueError("未找到权重列，且未启用等权兜底。")

    if score_col is not None:
        frame["__score"] = pd.to_numeric(frame[score_col], errors="coerce")
        frame = frame.sort_values(["__weight", "__score"], ascending=[False, False])
    else:
        frame = frame.sort_values(["__weight"], ascending=[False])

    frame = frame.head(max_names)

    positions: List[TargetPosition] = []
    for _, row in frame.iterrows():
        score_value = None
        if "__score" in row and pd.notna(row["__score"]):
            score_value = float(row["__score"])
        positions.append(
            TargetPosition(
                symbol=str(row["__symbol"]),
                target_weight=float(row["__weight"]),
                score=score_value,
                raw={str(k): (None if pd.isna(v) else v) for k, v in row.to_dict().items()},
            )
        )
    return portfolio_path, frame, positions


def build_price_map(
    portfolio_frame: pd.DataFrame,
    price_snapshot_path: str | None,
) -> Dict[str, float]:
    """从价格快照和持仓建议文件中构建价格映射。

    Args:
        portfolio_frame: 持仓建议表。
        price_snapshot_path: 外部价格快照路径，可为空。

    Returns:
        Dict[str, float]: 证券代码到价格的映射。
    """
    price_map: Dict[str, float] = {}
    price_dates: Dict[str, pd.Timestamp | None] = {}

    if price_snapshot_path:
        price_path = Path(price_snapshot_path)
        if price_path.exists():
            ext_frame = pd.read_csv(price_path)
            symbol_col = _find_column(ext_frame, SYMBOL_COLUMNS)
            price_col = _find_column(ext_frame, PRICE_COLUMNS)
            if symbol_col and price_col:
                ext_frame = ext_frame.copy()
                ext_frame["__symbol"] = ext_frame[symbol_col].map(normalize_symbol)
                ext_frame["__price"] = pd.to_numeric(ext_frame[price_col], errors="coerce")
                ext_date_col = _find_column(ext_frame, ["date", "trade_date", "price_date", "last_date"])
                ext_frame = ext_frame.dropna(subset=["__symbol", "__price"])
                ext_frame = ext_frame[ext_frame["__price"] > 0]
                for _, row in ext_frame.iterrows():
                    symbol = str(row["__symbol"])
                    price_map[symbol] = float(row["__price"])
                    price_dates[symbol] = _coerce_timestamp(row[ext_date_col]) if ext_date_col else None

    symbol_col = _find_column(portfolio_frame, SYMBOL_COLUMNS)
    if symbol_col:
        inner = portfolio_frame.copy()
        inner["__symbol"] = inner[symbol_col].map(normalize_symbol)
        inner = inner.dropna(subset=["__symbol"])
        for _, row in inner.iterrows():
            symbol = str(row["__symbol"])
            price, price_ts = _pick_portfolio_row_price(row)
            if price <= 0:
                continue
            existing_ts = price_dates.get(symbol)
            if existing_ts is None or (price_ts is not None and price_ts >= existing_ts):
                price_map[symbol] = price
                price_dates[symbol] = price_ts
            elif symbol not in price_map:
                price_map[symbol] = price

    return price_map
