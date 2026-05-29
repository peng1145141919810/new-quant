from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


def _bootstrap_repo() -> None:
    script_path = Path(__file__).resolve()
    package_root = script_path.parents[1] / "src" / "ashare"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))


_bootstrap_repo()

from engine.config_builder import build_runtime_config
from engine.industry_router.core.source_ingest import domain_from_url, resolve_official_page, strip_html
from engine.research_fact_store import (
    ensure_schema,
    insert_source_fetch_logs,
    register_default_field_lineage,
    resolve_research_fact_sqlite_path,
    sqlite_connection,
    upsert_rows,
)
from engine.tushare_client import TushareClient


FUTURE_SYMBOL_MAP: Dict[str, Dict[str, str]] = {
    "CU": {"industry_name": "有色", "sub_industry_name": "铜", "product_name": "铜"},
    "AL": {"industry_name": "有色", "sub_industry_name": "铝", "product_name": "铝"},
    "NI": {"industry_name": "有色", "sub_industry_name": "镍", "product_name": "镍"},
    "LC": {"industry_name": "新能源金属", "sub_industry_name": "锂", "product_name": "碳酸锂"},
    "SI": {"industry_name": "新能源金属", "sub_industry_name": "工业硅", "product_name": "工业硅"},
    "RB": {"industry_name": "钢铁", "sub_industry_name": "螺纹钢", "product_name": "螺纹钢"},
    "HC": {"industry_name": "钢铁", "sub_industry_name": "热卷", "product_name": "热轧卷板"},
    "I": {"industry_name": "钢铁", "sub_industry_name": "铁矿石", "product_name": "铁矿石"},
    "SC": {"industry_name": "能源", "sub_industry_name": "原油", "product_name": "原油"},
    "BU": {"industry_name": "能源", "sub_industry_name": "沥青", "product_name": "沥青"},
    "TA": {"industry_name": "化工", "sub_industry_name": "PTA", "product_name": "PTA"},
    "MA": {"industry_name": "化工", "sub_industry_name": "甲醇", "product_name": "甲醇"},
    "UR": {"industry_name": "化工", "sub_industry_name": "尿素", "product_name": "尿素"},
    "SA": {"industry_name": "化工", "sub_industry_name": "纯碱", "product_name": "纯碱"},
}


PPI_KEYWORD_MAP: Dict[str, Dict[str, str]] = {
    "mdi": {"industry_name": "化工", "sub_industry_name": "MDI", "product_name": "MDI"},
    "px": {"industry_name": "化工", "sub_industry_name": "PX", "product_name": "PX"},
    "pta": {"industry_name": "化工", "sub_industry_name": "PTA", "product_name": "PTA"},
    "甲醇": {"industry_name": "化工", "sub_industry_name": "甲醇", "product_name": "甲醇"},
    "苯乙烯": {"industry_name": "化工", "sub_industry_name": "苯乙烯", "product_name": "苯乙烯"},
    "烧碱": {"industry_name": "化工", "sub_industry_name": "烧碱", "product_name": "烧碱"},
    "纯碱": {"industry_name": "化工", "sub_industry_name": "纯碱", "product_name": "纯碱"},
    "尿素": {"industry_name": "化工", "sub_industry_name": "尿素", "product_name": "尿素"},
    "碳酸锂": {"industry_name": "新能源金属", "sub_industry_name": "锂", "product_name": "碳酸锂"},
    "工业硅": {"industry_name": "新能源金属", "sub_industry_name": "工业硅", "product_name": "工业硅"},
    "锂电池": {"industry_name": "新能源金属", "sub_industry_name": "锂电", "product_name": "锂电池"},
    "镍": {"industry_name": "有色", "sub_industry_name": "镍", "product_name": "镍"},
    "铜": {"industry_name": "有色", "sub_industry_name": "铜", "product_name": "铜"},
    "铝": {"industry_name": "有色", "sub_industry_name": "铝", "product_name": "铝"},
    "氧化铝": {"industry_name": "有色", "sub_industry_name": "氧化铝", "product_name": "氧化铝"},
    "电解铜": {"industry_name": "有色", "sub_industry_name": "铜", "product_name": "电解铜"},
    "电解铝": {"industry_name": "有色", "sub_industry_name": "铝", "product_name": "电解铝"},
    "集成电路": {"industry_name": "电子", "sub_industry_name": "半导体", "product_name": "集成电路"},
    "服务器": {"industry_name": "电子", "sub_industry_name": "算力硬件", "product_name": "服务器"},
    "手机": {"industry_name": "电子", "sub_industry_name": "消费电子", "product_name": "手机"},
    "晶圆": {"industry_name": "电子", "sub_industry_name": "半导体", "product_name": "晶圆"},
    "硅片": {"industry_name": "新能源金属", "sub_industry_name": "光伏材料", "product_name": "硅片"},
    "多晶硅": {"industry_name": "新能源金属", "sub_industry_name": "光伏材料", "product_name": "多晶硅"},
    "硅铁": {"industry_name": "钢铁", "sub_industry_name": "硅铁", "product_name": "硅铁"},
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stable_id(*parts: Any) -> str:
    seed = "||".join(_text(item) for item in parts if _text(item))
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]


def _normalize_date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            return ""
    return ""


def _log_id(*parts: Any) -> str:
    return hashlib.md5("||".join(_text(item) for item in parts).encode("utf-8")).hexdigest()[:24]


def _load_affordable_payloads(db_path: Path, dataset: str) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT payload_json FROM affordable_dataset_rows WHERE dataset = ?", (dataset,)).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _direction_from_title(title: str) -> str:
    low = _text(title).lower()
    positives = ["上涨", "收涨", "偏强", "走强", "上行", "回暖", "紧张", "支撑"]
    negatives = ["下跌", "收跌", "偏弱", "走弱", "下行", "承压", "回落", "累库"]
    pos = sum(1 for word in positives if word in low)
    neg = sum(1 for word in negatives if word in low)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _match_ppi_meta(title: str) -> Dict[str, str]:
    low = _text(title).lower()
    for keyword, meta in PPI_KEYWORD_MAP.items():
        if keyword.lower() in low:
            return dict(meta)
    return {"industry_name": "大宗商品", "sub_industry_name": "", "product_name": ""}


def _standardize_industry_meta(text: str, default_industry: str = "综合工业") -> Dict[str, str]:
    meta = _match_ppi_meta(text)
    if _text(meta.get("industry_name")) and meta["industry_name"] != "大宗商品":
        return meta
    blob = _text(text).lower()
    if any(token in blob for token in ["电子", "集成电路", "手机", "服务器", "算力", "晶圆"]):
        product = "集成电路" if "集成电路" in blob else ("手机" if "手机" in blob else ("服务器" if "服务器" in blob else ""))
        return {"industry_name": "电子", "sub_industry_name": "半导体" if product == "集成电路" else "消费电子" if product == "手机" else "算力硬件" if product == "服务器" else "", "product_name": product}
    if any(token in blob for token in ["锂", "碳酸锂", "工业硅", "锂电", "多晶硅", "硅片", "光伏"]):
        product = "碳酸锂" if "碳酸锂" in blob else "工业硅" if "工业硅" in blob else "锂电池" if "锂电" in blob else "多晶硅" if "多晶硅" in blob else "硅片" if "硅片" in blob else ""
        sub = "锂" if any(token in blob for token in ["锂", "碳酸锂", "锂电"]) else "光伏材料"
        return {"industry_name": "新能源金属", "sub_industry_name": sub, "product_name": product}
    if any(token in blob for token in ["铜", "铝", "镍", "氧化铝", "电解铜", "电解铝", "有色"]):
        product = "电解铜" if "电解铜" in blob else "电解铝" if "电解铝" in blob else "氧化铝" if "氧化铝" in blob else "铜" if "铜" in blob else "铝" if "铝" in blob else "镍" if "镍" in blob else ""
        sub = "铜" if "铜" in product else "铝" if "铝" in product else "镍" if "镍" in product else ""
        return {"industry_name": "有色", "sub_industry_name": sub, "product_name": product}
    if any(token in blob for token in ["化工", "mdi", "px", "pta", "甲醇", "尿素", "纯碱", "烧碱", "苯乙烯", "石化"]):
        product = "MDI" if "mdi" in blob else "PX" if "px" in blob else "PTA" if "pta" in blob else "甲醇" if "甲醇" in blob else "尿素" if "尿素" in blob else "纯碱" if "纯碱" in blob else "烧碱" if "烧碱" in blob else "苯乙烯" if "苯乙烯" in blob else ""
        return {"industry_name": "化工", "sub_industry_name": product, "product_name": product}
    return {"industry_name": default_industry, "sub_industry_name": "", "product_name": ""}


def _build_ppi_rows(affordable_db: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for payload in _load_affordable_payloads(affordable_db, "ppi_market_digest"):
        title = _text(payload.get("title"))
        meta = _standardize_industry_meta(title, default_industry="大宗商品")
        trade_date = _normalize_date(payload.get("as_of_date")) or datetime.now().strftime("%Y-%m-%d")
        row_id = f"ppi::{trade_date}::{_stable_id(title)}"
        rows.append(
            {
                "row_id": row_id,
                "trade_date": trade_date,
                "industry_name": meta["industry_name"],
                "sub_industry_name": meta["sub_industry_name"],
                "product_name": meta["product_name"],
                "factor_type": "price_digest",
                "factor_subtype": _text(payload.get("digest_type") or "digest"),
                "value_raw": title[:220],
                "value_num": "",
                "unit": "",
                "direction_hint": _direction_from_title(title),
                "source_name": "100ppi_market_digest",
                "source_url": _text(payload.get("source_url")),
                "source_class": "public_web_research",
                "publish_date": trade_date,
                "is_official_source": 0,
                "raw_payload_path": f"{affordable_db}::ppi_market_digest::{_stable_id(title)}",
            }
        )
    return rows


def _parse_growth_value(text: str) -> float | None:
    match = pd.Series([text]).str.extract(r"(-?\d+(?:\.\d+)?)%").iloc[0, 0]
    if match in (None, ""):
        return None
    try:
        return float(match)
    except Exception:
        return None


def _build_customs_rows(affordable_db: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for payload in _load_affordable_payloads(affordable_db, "customs_summary"):
        publish_date = _normalize_date(payload.get("release_date"))
        trade_date = publish_date
        description = _text(payload.get("description"))
        yoy_total = _parse_growth_value(_text(payload.get("yoy_total_pct")) or description)
        yoy_export = _parse_growth_value(_text(payload.get("yoy_export_pct")) or description)
        yoy_import = _parse_growth_value(_text(payload.get("yoy_import_pct")) or description)
        candidates = [
            ("total_trade", "货物贸易", "total", _text(payload.get("total_trade_value")), yoy_total, _text(payload.get("total_trade_unit"))),
            ("export", "出口链", "export", _text(payload.get("export_value")), yoy_export, _text(payload.get("total_trade_unit"))),
            ("import", "上游资源", "import", _text(payload.get("import_value")), yoy_import, _text(payload.get("total_trade_unit"))),
        ]
        for key, industry_name, flag, value_raw, yoy_value, unit in candidates:
            if not value_raw and yoy_value is None:
                continue
            rows.append(
                {
                    "row_id": f"customs::{publish_date}::{key}",
                    "trade_date": trade_date,
                    "publish_date": publish_date,
                    "region_scope": "全国",
                    "industry_name": industry_name,
                    "product_name": "",
                    "import_export_flag": flag,
                    "value_raw": value_raw or description[:220],
                    "value_num": yoy_value if yoy_value is not None else "",
                    "unit": "%" if yoy_value is not None else unit,
                    "direction_hint": "positive" if (yoy_value or 0.0) > 0 else ("negative" if (yoy_value or 0.0) < 0 else "neutral"),
                    "source_name": "gov_cn_customs_summary",
                    "source_url": _text(payload.get("source_url")),
                    "source_class": "official_truth",
                    "raw_payload_path": f"{affordable_db}::customs_summary::{publish_date}",
                }
            )
    return rows


def _latest_trade_dates(client: TushareClient, lookback_days: int) -> List[str]:
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - pd.Timedelta(days=max(7, lookback_days * 3))).strftime("%Y%m%d")
    trade_cal = client.call("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
    if trade_cal.empty:
        return []
    trade_cal["cal_date"] = trade_cal["cal_date"].astype(str)
    open_days = trade_cal.loc[pd.to_numeric(trade_cal["is_open"], errors="coerce").fillna(0).gt(0), "cal_date"].tolist()
    return sorted(open_days)[-lookback_days:]


def _root_future_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["symbol_root"] = frame["ts_code"].astype(str).str.extract(r"^([A-Z]+)")
    frame["is_root"] = ~frame["ts_code"].astype(str).str.contains(r"\d")
    chosen = frame.loc[frame["is_root"]].copy()
    if chosen.empty:
        chosen = frame.sort_values("vol", ascending=False).drop_duplicates(subset=["symbol_root"], keep="first")
    return chosen


def _build_futures_rows(client: TushareClient, lookback_days: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for trade_date in _latest_trade_dates(client, lookback_days):
        wsr = client.call("fut_wsr", trade_date=trade_date)
        if not wsr.empty:
            agg = (
                wsr.loc[wsr["symbol"].astype(str).isin(FUTURE_SYMBOL_MAP.keys())]
                .groupby("symbol", as_index=False)[["vol", "vol_chg"]]
                .sum()
            )
            for _, item in agg.iterrows():
                symbol = _text(item.get("symbol"))
                meta = FUTURE_SYMBOL_MAP[symbol]
                direction = "positive" if float(item.get("vol_chg") or 0.0) < 0 else ("negative" if float(item.get("vol_chg") or 0.0) > 0 else "neutral")
                rows.append(
                    {
                        "row_id": f"wsr::{trade_date}::{symbol}::delta",
                        "trade_date": _normalize_date(trade_date),
                        "industry_name": meta["industry_name"],
                        "sub_industry_name": meta["sub_industry_name"],
                        "product_name": meta["product_name"],
                        "factor_type": "warehouse_receipt",
                        "factor_subtype": "warehouse_receipt_delta",
                        "value_raw": f"vol={float(item.get('vol') or 0.0)},vol_chg={float(item.get('vol_chg') or 0.0)}",
                        "value_num": float(item.get("vol_chg") or 0.0),
                        "unit": "",
                        "direction_hint": direction,
                        "source_name": "tushare.fut_wsr",
                        "source_url": "",
                        "source_class": "derived_from_truth",
                        "publish_date": _normalize_date(trade_date),
                        "is_official_source": 0,
                        "raw_payload_path": "tushare::fut_wsr",
                    }
                )
        daily = client.call("fut_daily", trade_date=trade_date)
        if not daily.empty:
            chosen = _root_future_rows(daily.loc[daily["ts_code"].astype(str).str.extract(r"^([A-Z]+)")[0].isin(FUTURE_SYMBOL_MAP.keys())].copy())
            for _, item in chosen.iterrows():
                symbol = _text(item.get("symbol_root"))
                meta = FUTURE_SYMBOL_MAP.get(symbol)
                if not meta:
                    continue
                pre_settle = float(item.get("pre_settle") or 0.0)
                settle = float(item.get("settle") or item.get("close") or 0.0)
                pct = ((settle / pre_settle) - 1.0) * 100.0 if pre_settle else 0.0
                rows.append(
                    {
                        "row_id": f"fut::{trade_date}::{symbol}::settle_pct",
                        "trade_date": _normalize_date(trade_date),
                        "industry_name": meta["industry_name"],
                        "sub_industry_name": meta["sub_industry_name"],
                        "product_name": meta["product_name"],
                        "factor_type": "futures_price",
                        "factor_subtype": "settle_pct_change",
                        "value_raw": f"settle={settle},pre_settle={pre_settle}",
                        "value_num": round(pct, 4),
                        "unit": "%",
                        "direction_hint": "positive" if pct > 0 else ("negative" if pct < 0 else "neutral"),
                        "source_name": "tushare.fut_daily",
                        "source_url": "",
                        "source_class": "derived_from_truth",
                        "publish_date": _normalize_date(trade_date),
                        "is_official_source": 0,
                        "raw_payload_path": "tushare::fut_daily",
                    }
                )
    return rows


def _parse_float_like(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    match = pd.Series([text]).str.extract(r"(-?\d+(?:\.\d+)?)").iloc[0, 0]
    if match in (None, ""):
        return None
    try:
        return float(match)
    except Exception:
        return None


def _extract_sentences(text: str) -> List[str]:
    items = [_text(item) for item in re.split(r"[??;\n]", text) if _text(item)]
    return items


def _material_price_rows(source: Dict[str, Any], html: str, publish_date: str, title: str) -> List[Dict[str, Any]]:
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for table in tables:
        cols = [str(col) for col in table.columns]
        if not any(("??" in col or "??" in col) for col in cols):
            continue
        if not any(("??" in col or "??" in col or "??" in col) for col in cols):
            continue
        product_col = next((col for col in cols if "??" in col or "??" in col), cols[0])
        price_col = next((col for col in cols if "??" in col), "")
        change_col = next((col for col in cols if "???" in col or "??" in col), "")
        unit_col = next((col for col in cols if "??" in col), "")
        current_industry = "大宗商品"
        for _, row in table.iterrows():
            product_name = _text(row.get(product_col))
            if not product_name or product_name in {"????", "????"}:
                continue
            raw_price = _text(row.get(price_col)) if price_col else ""
            raw_change = _text(row.get(change_col)) if change_col else ""
            if not raw_price and not raw_change:
                current_industry = _text(_standardize_industry_meta(product_name, default_industry=product_name[:40]).get("industry_name")) or product_name[:40]
                continue
            change_num = _parse_float_like(raw_change)
            direction = "neutral"
            if change_num is not None:
                direction = "positive" if change_num > 0 else ("negative" if change_num < 0 else "neutral")
            meta = _standardize_industry_meta(f"{current_industry} {product_name}", default_industry=current_industry or "大宗商品")
            out.append(
                {
                    "row_id": f"official_price::nbs_material_prices::{publish_date}::{_stable_id(current_industry, product_name, raw_price, raw_change)}",
                    "trade_date": publish_date,
                    "industry_name": _text(meta.get("industry_name")) or current_industry,
                    "sub_industry_name": _text(meta.get("sub_industry_name")) or title[:40],
                    "product_name": _text(meta.get("product_name")) or product_name,
                    "factor_type": "spot_price",
                    "factor_subtype": _text(source.get("source_id")),
                    "value_raw": raw_price or raw_change or product_name,
                    "value_num": change_num if change_num is not None else (_parse_float_like(raw_price) or ""),
                    "unit": "%" if change_num is not None else _text(row.get(unit_col)) if unit_col else "",
                    "direction_hint": direction,
                    "source_name": _text(source.get("source_name")),
                    "source_url": _text(source.get("url")),
                    "source_class": "official_truth",
                    "publish_date": publish_date,
                    "is_official_source": 1,
                    "raw_payload_path": f"{_text(source.get('url'))}#{_stable_id(product_name, raw_price, raw_change)}",
                }
            )
    return out


def _infer_official_fields(source: Dict[str, Any], title: str, sentence: str) -> tuple[str, str, str]:
    blob = f"{_text(source.get('source_id'))} {title} {sentence}"
    meta = _standardize_industry_meta(blob, default_industry="综合工业")
    industry_name = _text(meta.get("industry_name")) or "综合工业"
    sub_industry_name = _text(meta.get("sub_industry_name")) or title[:40]
    product_name = _text(meta.get("product_name"))
    if industry_name in {"电子", "新能源金属", "有色", "化工"}:
        return industry_name, sub_industry_name, product_name
    if any(token in blob.lower() for token in ["电力", "发电", "用电", "煤"]):
        return "能源", title[:40], product_name or "电力"
    if any(token in blob.lower() for token in ["钢", "铁矿", "螺纹"]):
        return "钢铁", title[:40], product_name
    return industry_name, sub_industry_name, product_name


def _metric_bucket(mechanism: str, source: Dict[str, Any], sentence: str) -> str:
    source_id = _text(source.get("source_id")).lower()
    text = sentence.lower()
    if source_id == "nbs_material_prices":
        return "price"
    if any(token in text for token in ["价格", "库存", "仓单", "ppi", "涨跌", "均价"]):
        return "price"
    if any(token in text for token in ["产量", "利用率", "开工率", "投资", "出口", "出货量", "用电量", "增加值"]):
        return "operation"
    return "price" if mechanism == "price_inventory" else "operation"


def _build_official_metric_rows(
    contract_root: Path,
    *,
    as_of_date: str,
    config: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    payload = json.loads((contract_root / "source_contracts.json").read_text(encoding="utf-8-sig"))
    price_rows: List[Dict[str, Any]] = []
    operation_rows: List[Dict[str, Any]] = []
    fetch_logs: List[Dict[str, Any]] = []
    seen_price: set[str] = set()
    seen_operation: set[str] = set()
    for mechanism, bucket in dict(payload.get("mechanism_groups", {}) or {}).items():
        for category in ["industry_state_sources", "macro_context_sources"]:
            for source in list(dict(bucket).get(category, []) or []):
                if _text(source.get("mode")) != "official_page":
                    continue
                started_at = datetime.now()
                price_before = len(price_rows)
                operation_before = len(operation_rows)
                try:
                    resolved = resolve_official_page(source, timeout=15, as_of_date=as_of_date, config=config)
                    html = _text(resolved.get("html"))
                except Exception as exc:
                    fetch_logs.append(
                        {
                            "log_id": _log_id("industry_hard_factor", mechanism, category, _text(source.get("source_id")), started_at.isoformat()),
                            "run_id": "",
                            "pipeline_name": "industry_hard_factor_refresh",
                            "dataset_name": category,
                            "source_id": _text(source.get("source_id")),
                            "source_name": _text(source.get("source_name")),
                            "source_url": _text(source.get("url")),
                            "source_domain": domain_from_url(_text(source.get("url"))),
                            "trade_date": as_of_date,
                            "publish_date": "",
                            "status": "failed",
                            "rows_written": 0,
                            "items_seen": 0,
                            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
                            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "latency_ms": int((datetime.now() - started_at).total_seconds() * 1000),
                            "error_class": exc.__class__.__name__,
                            "message": str(exc)[:300],
                            "artifact_path": "",
                            "params_json": json.dumps({"mechanism": mechanism, "category": category}, ensure_ascii=False),
                            "extra_json": "",
                            "is_stale": 1,
                            "freshness_days": None,
                        }
                    )
                    continue
                publish_date = _normalize_date(resolved.get("publish_date")) or datetime.now().strftime("%Y-%m-%d")
                title = _text(resolved.get("title") or source.get("source_name"))
                resolved_url = _text(resolved.get("resolved_url") or source.get("url"))
                if _text(source.get("source_id")) == "nbs_material_prices":
                    for row in _material_price_rows(source, html, publish_date, title):
                        row_id = _text(row.get("row_id"))
                        if row_id and row_id not in seen_price:
                            seen_price.add(row_id)
                            price_rows.append(row)
                text = strip_html(html)
                sentences = _extract_sentences(text)[:120]
                for idx, sentence in enumerate(sentences[:30], start=1):
                    if not any(token in sentence for token in ["同比", "增长", "下降", "回升", "回落", "利用率", "价格", "库存", "仓单", "%"]):
                        continue
                    value_num = _parse_growth_value(sentence)
                    direction = "positive" if any(token in sentence for token in ["增长", "回升", "改善"]) else ("negative" if any(token in sentence for token in ["下降", "回落", "承压"]) else "neutral")
                    industry_name, sub_industry_name, product_name = _infer_official_fields(source, title, sentence)
                    bucket_name = _metric_bucket(mechanism, source, sentence)
                    if bucket_name == "price":
                        row_id = f"official_price::{mechanism}::{publish_date}::{idx}::{_stable_id(_text(source.get('source_id')), sentence)}"
                        if row_id in seen_price:
                            continue
                        seen_price.add(row_id)
                        price_rows.append(
                            {
                                "row_id": row_id,
                                "trade_date": publish_date,
                                "industry_name": industry_name,
                                "sub_industry_name": sub_industry_name,
                                "product_name": product_name,
                                "factor_type": "official_price_index" if any(token in sentence for token in ["价格", "PPI", "%"]) else "macro_demand_signal",
                                "factor_subtype": _text(source.get("source_id")),
                                "value_raw": sentence[:220],
                                "value_num": value_num if value_num is not None else "",
                                "unit": "%" if value_num is not None else "",
                                "direction_hint": direction,
                                "source_name": _text(source.get("source_name")),
                                "source_url": resolved_url,
                                "source_class": "official_truth",
                                "publish_date": publish_date,
                                "is_official_source": 1,
                                "raw_payload_path": f"{resolved_url}#{idx}",
                            }
                        )
                    else:
                        op_type = "industry_output"
                        if "投资" in sentence:
                            op_type = "investment_growth"
                        elif "出口" in sentence:
                            op_type = "export_growth"
                        elif any(token in sentence for token in ["产能利用率", "利用率", "开工率"]):
                            op_type = "capacity_utilization"
                        elif any(token in sentence for token in ["用电量", "发电量"]):
                            op_type = "power_demand"
                        row_id = f"official_op::{mechanism}::{publish_date}::{idx}::{_stable_id(_text(source.get('source_id')), sentence)}"
                        if row_id in seen_operation:
                            continue
                        seen_operation.add(row_id)
                        operation_rows.append(
                            {
                                "row_id": row_id,
                                "trade_date": publish_date,
                                "industry_name": industry_name,
                                "sub_industry_name": sub_industry_name,
                                "product_name": product_name,
                                "operation_type": op_type,
                                "value_raw": sentence[:220],
                                "value_num": value_num if value_num is not None else "",
                                "unit": "%" if value_num is not None else "",
                                "direction_hint": direction,
                                "source_name": _text(source.get("source_name")),
                                "source_url": resolved_url,
                                "source_class": "official_truth",
                                "publish_date": publish_date,
                                "raw_payload_path": f"{resolved_url}#{idx}",
                            }
                        )
                freshness_days = None
                if publish_date:
                    try:
                        freshness_days = (datetime.strptime(as_of_date, "%Y-%m-%d").date() - datetime.strptime(publish_date, "%Y-%m-%d").date()).days
                    except Exception:
                        freshness_days = None
                fetch_logs.append(
                    {
                        "log_id": _log_id("industry_hard_factor", mechanism, category, _text(source.get("source_id")), publish_date, started_at.isoformat()),
                        "run_id": "",
                        "pipeline_name": "industry_hard_factor_refresh",
                        "dataset_name": category,
                        "source_id": _text(source.get("source_id")),
                        "source_name": _text(source.get("source_name")),
                        "source_url": resolved_url,
                        "source_domain": domain_from_url(resolved_url),
                        "trade_date": as_of_date,
                        "publish_date": publish_date,
                        "status": "success",
                        "rows_written": (len(price_rows) - price_before) + (len(operation_rows) - operation_before),
                        "items_seen": int(resolved.get("candidate_count") or 1),
                        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "latency_ms": int((datetime.now() - started_at).total_seconds() * 1000),
                        "error_class": "",
                        "message": title[:220],
                        "artifact_path": "",
                        "params_json": json.dumps({"mechanism": mechanism, "category": category}, ensure_ascii=False),
                        "extra_json": json.dumps(
                            {
                                "resolved_from": _text(source.get("url")),
                                "resolved_title": title,
                                "llm_selected": bool(resolved.get("llm_selected", False)),
                                "llm_provider": _text(resolved.get("llm_provider")),
                                "llm_model": _text(resolved.get("llm_model")),
                                "llm_confidence": resolved.get("llm_confidence", ""),
                                "llm_reason": _text(resolved.get("llm_reason")),
                            },
                            ensure_ascii=False,
                        ),
                        "is_stale": 1 if freshness_days is not None and freshness_days > 45 else 0,
                        "freshness_days": freshness_days,
                    }
                )
    return price_rows, operation_rows, fetch_logs


def main() -> int:
    parser = argparse.ArgumentParser(description="Build research-side industry hard-factor tables.")
    parser.add_argument("--db-path", default="", help="Override research fact sqlite path.")
    parser.add_argument("--lookback-days", type=int, default=3, help="Recent open trade days for futures / warehouse receipt ingestion.")
    args = parser.parse_args()

    config = build_runtime_config()
    db_path = Path(args.db_path).resolve() if args.db_path else resolve_research_fact_sqlite_path(config)
    affordable_db = Path(str(config.get("paths", {}).get("affordable_sqlite_path", "") or "")).resolve()
    contract_root = Path(str(config.get("industry_router", {}).get("contract_root", "") or "")).resolve()
    client = TushareClient(dict(config.get("providers", {}).get("tushare", {}) or {}))

    price_rows = _build_ppi_rows(affordable_db)
    price_rows.extend(_build_futures_rows(client, args.lookback_days) if client.enabled() else [])
    as_of_date = datetime.now().strftime("%Y-%m-%d")
    official_price_rows, operation_rows, fetch_logs = _build_official_metric_rows(
        contract_root,
        as_of_date=as_of_date,
        config=config,
    )
    price_rows.extend(official_price_rows)
    customs_rows = _build_customs_rows(affordable_db)

    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        register_default_field_lineage(conn)
        price_upserted = upsert_rows(conn, "industry_factor_price_inventory_daily", price_rows, ("row_id",))
        operation_upserted = upsert_rows(conn, "industry_factor_operation_daily", operation_rows, ("row_id",))
        customs_upserted = upsert_rows(conn, "industry_factor_customs_summary_daily", customs_rows, ("row_id",))
        insert_source_fetch_logs(conn, fetch_logs)
        counts = {
            "industry_factor_price_inventory_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_price_inventory_daily").fetchone()[0],
            "industry_factor_operation_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_operation_daily").fetchone()[0],
            "industry_factor_customs_summary_daily": conn.execute("SELECT COUNT(*) FROM industry_factor_customs_summary_daily").fetchone()[0],
        }

    summary = {
        "db_path": str(db_path),
        "affordable_db": str(affordable_db),
        "rows_upserted": {
            "industry_factor_price_inventory_daily": price_upserted,
            "industry_factor_operation_daily": operation_upserted,
            "industry_factor_customs_summary_daily": customs_upserted,
        },
        "source_fetch_logs_written": len(fetch_logs),
        "table_counts": counts,
        "tushare_enabled": client.enabled(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
