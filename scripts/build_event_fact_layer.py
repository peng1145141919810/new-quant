from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple


def _bootstrap_repo() -> None:
    script_path = Path(__file__).resolve()
    package_root = script_path.parents[1] / "src" / "ashare"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))


_bootstrap_repo()

from engine.config_builder import build_runtime_config
from engine.research_fact_store import (
    ensure_schema,
    insert_source_fetch_logs,
    register_default_field_lineage,
    resolve_manual_event_proxy_path,
    resolve_research_fact_sqlite_path,
    sqlite_connection,
    upsert_rows,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stable_id(*parts: Any) -> str:
    seed = "||".join(_text(item) for item in parts if _text(item))
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]


def _build_fetch_log(
    *,
    run_id: str,
    dataset_name: str,
    source_name: str,
    source_url: str,
    started_at: datetime,
    finished_at: datetime,
    rows_written: int,
    items_seen: int,
    message: str,
) -> Dict[str, Any]:
    return {
        "log_id": f"event_fact::{run_id}::{dataset_name}",
        "run_id": run_id,
        "pipeline_name": "research_fact_refresh",
        "dataset_name": dataset_name,
        "source_id": dataset_name,
        "source_name": source_name,
        "source_url": source_url,
        "source_domain": "",
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "publish_date": "",
        "status": "success",
        "rows_written": rows_written,
        "items_seen": items_seen,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": int((finished_at - started_at).total_seconds() * 1000),
        "error_class": "",
        "message": message[:300],
        "artifact_path": "",
        "params_json": "",
        "extra_json": "",
        "is_stale": 0,
        "freshness_days": None,
    }


def _normalize_date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
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


def _normalize_symbol(code: Any) -> str:
    text = _text(code).upper()
    if not text or text == "UNKNOWN":
        return ""
    if "." in text:
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return ""
    if digits.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{digits}.SH"
    if digits.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return f"{digits}.SZ"
    if digits.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return f"{digits}.BJ"
    return ""


def _simplify_company_name(name: Any) -> str:
    text = _text(name)
    if not text:
        return ""
    for token in ["股份有限公司", "有限责任公司", "有限公司", "集团股份公司", "股份公司", "集团", "公司"]:
        text = text.replace(token, "")
    return text.strip()


def _amount_from_text(value: Any) -> tuple[str, float | None, str]:
    text = _text(value).replace(",", "")
    if not text:
        return "", None, ""
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(亿元人民币|亿元|万元人民币|万元|元人民币|元)", text)
    if not match:
        return text, None, ""
    amount = float(match.group(1))
    unit = match.group(2)
    factor = {
        "亿元人民币": 1e8,
        "亿元": 1e8,
        "万元人民币": 1e4,
        "万元": 1e4,
        "元人民币": 1.0,
        "元": 1.0,
    }.get(unit, 1.0)
    return text, round(amount * factor, 2), unit


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return _text(value)


def _direction_from_text(blob: str, *, positive_words: Iterable[str], negative_words: Iterable[str], default: str = "positive") -> str:
    low = blob.lower()
    pos = sum(1 for word in positive_words if word and word.lower() in low)
    neg = sum(1 for word in negative_words if word and word.lower() in low)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return default


def _importance_level(*, amount_cny: float | None, source_confidence: float, major_hint: bool = False) -> tuple[str, int]:
    if major_hint or (amount_cny is not None and amount_cny >= 5e8) or source_confidence >= 0.93:
        return "critical", 1
    if (amount_cny is not None and amount_cny >= 1e8) or source_confidence >= 0.82:
        return "high", 1
    if source_confidence >= 0.66:
        return "medium", 0
    return "low", 0


def _load_affordable_payloads(db_path: Path, dataset: str) -> List[Dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM affordable_dataset_rows
            WHERE dataset = ?
            """,
            (dataset,),
        ).fetchall()
    finally:
        conn.close()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _candidate_event_store_paths(config: Dict[str, Any]) -> List[Path]:
    paths: List[Path] = []
    local_root_raw = str(config.get("paths", {}).get("event_store_root", "") or "").strip()
    if local_root_raw:
        paths.append(Path(local_root_raw).resolve() / "event_store.jsonl")
    paths.append(Path(r"F:\quant_data\Ashare\data\event_lake_v6\curated\event_store.jsonl"))
    out: List[Path] = []
    for path in paths:
        if path.exists() and path not in out:
            out.append(path)
    return out


def _iter_event_store_rows(paths: List[Path], lookback_days: int) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    cutoff = datetime.now().date() - timedelta(days=max(1, int(lookback_days or 45)))
    seen: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                event_id = _text(payload.get("event_id"))
                if event_id and event_id in seen:
                    continue
                publish_date = _normalize_date(payload.get("publish_time") or payload.get("crawl_time") or dict(payload.get("structured_facts", {}) or {}).get("announcement_date"))
                if publish_date:
                    try:
                        if datetime.strptime(publish_date, "%Y-%m-%d").date() < cutoff:
                            continue
                    except Exception:
                        pass
                if event_id:
                    seen.add(event_id)
                yield path, payload


def _load_stock_lookup(affordable_db: Path) -> tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    rows = _load_affordable_payloads(affordable_db, "stock_basic")
    by_symbol: Dict[str, Dict[str, str]] = {}
    by_name: Dict[str, Dict[str, str]] = {}
    by_simple_name: Dict[str, Dict[str, str]] = {}
    for row in rows:
        ts_code = _text(row.get("ts_code")).upper()
        symbol = _normalize_symbol(ts_code or row.get("symbol"))
        code = _text(row.get("symbol")) if _text(row.get("symbol")).isdigit() else ""
        name = _text(row.get("name"))
        if not symbol or not name:
            continue
        payload = {"symbol": symbol, "ts_code": ts_code or symbol, "company_name": name, "code": code}
        by_symbol[symbol] = payload
        if code:
            by_symbol[code] = payload
        by_name.setdefault(name, payload)
        simple = _simplify_company_name(name)
        if simple and simple not in by_simple_name:
            by_simple_name[simple] = payload
    return by_symbol, by_name, by_simple_name


def _resolve_identity(
    *,
    security_code: Any,
    company_name: Any,
    facts: Dict[str, Any],
    by_symbol: Dict[str, Dict[str, str]],
    by_name: Dict[str, Dict[str, str]],
    by_simple_name: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    symbol = _normalize_symbol(security_code)
    if symbol and symbol in by_symbol:
        return dict(by_symbol[symbol])
    digits = "".join(ch for ch in _text(security_code) if ch.isdigit())
    if digits and digits in by_symbol:
        return dict(by_symbol[digits])
    for candidate in [
        _text(company_name),
        _text(facts.get("subject")),
        _text(facts.get("entity")),
        _text(facts.get("recipient")),
        _text(facts.get("subsidiary")),
    ]:
        if candidate and candidate in by_name:
            return dict(by_name[candidate])
        simple = _simplify_company_name(candidate)
        if simple and simple in by_simple_name:
            return dict(by_simple_name[simple])
    return {
        "symbol": symbol,
        "ts_code": symbol,
        "company_name": _text(company_name) or _text(facts.get("subject")) or _text(facts.get("entity")),
        "code": digits,
    }


def _base_source_fields(raw: Dict[str, Any], source_path: Path) -> Dict[str, Any]:
    source_type = _text(raw.get("source_type")).lower()
    source_name = _text(raw.get("source_name"))
    url = _text(dict(raw.get("structured_facts", {}) or {}).get("url"))
    source_class = "public_web_research"
    source_authority = "event_store_curated"
    is_issuer = 0
    is_official = 0
    if source_type == "announcement" or any(token in source_name.lower() for token in ["cninfo", "sse", "szse"]):
        source_class = "issuer_disclosure_truth"
        source_authority = source_name or "issuer_disclosure"
        is_issuer = 1
    elif "gov" in url.lower() or "gov" in source_name.lower():
        source_class = "official_truth"
        source_authority = source_name or "gov_official"
        is_official = 1
    elif any(token in url.lower() for token in ["sse.com.cn", "szse.cn", "shfe.com.cn", "dce.com.cn", "czce.com.cn"]):
        source_class = "exchange_truth"
        source_authority = source_name or "exchange_page"
    confidence = 0.62
    if is_issuer:
        confidence = 0.92
    elif source_class == "official_truth":
        confidence = 0.88
    return {
        "raw_source_name": source_name or source_type or source_path.stem,
        "raw_source_url": url,
        "source_class": source_class,
        "source_authority": source_authority,
        "source_confidence": confidence,
        "is_issuer_disclosure": is_issuer,
        "is_official_source": is_official,
        "is_structured_from_text": 1,
    }


def _classify_event(raw: Dict[str, Any]) -> str:
    facts = dict(raw.get("structured_facts", {}) or {})
    event_type = _text(raw.get("event_type")).lower()
    blob = " ".join(
        [
            event_type,
            _text(raw.get("raw_title")),
            _text(raw.get("summary")),
            _json_text(facts),
        ]
    ).lower()
    if event_type in {"contract_award", "major_contract", "招标", "招标公告"}:
        return "contract_order"
    if any(token in blob for token in ["中标", "订单", "合同", "开发定点", "框架协议", "采购项目", "bid", "award"]) and not any(token in blob for token in ["募集资金", "监管协议"]):
        return "contract_order"
    if event_type == "price_change":
        return "price_supply"
    if any(token in blob for token in ["涨价", "提价", "降价", "停产", "限产", "检修", "复工复产", "库存", "仓单", "供给扰动"]):
        return "price_supply"
    if event_type in {"investment", "business_development", "operational_change"} and any(
        token in blob for token in ["扩产", "产能", "投产", "项目", "技改", "增资", "建设", "capex", "设备", "产能提升", "复工"]
    ):
        return "capacity_capex"
    return ""


def _build_company_action_from_event(
    raw: Dict[str, Any],
    source_path: Path,
    *,
    by_symbol: Dict[str, Dict[str, str]],
    by_name: Dict[str, Dict[str, str]],
    by_simple_name: Dict[str, Dict[str, str]],
) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None, Dict[str, Any] | None]:
    facts = dict(raw.get("structured_facts", {}) or {})
    event_bucket = _classify_event(raw)
    if not event_bucket:
        return None, None, None
    identity = _resolve_identity(
        security_code=raw.get("security_code"),
        company_name=raw.get("company_name"),
        facts=facts,
        by_symbol=by_symbol,
        by_name=by_name,
        by_simple_name=by_simple_name,
    )
    amount_text = _text(facts.get("contract_amount") or facts.get("investment_amount") or facts.get("amount") or facts.get("project_amount"))
    amount_raw, amount_cny, unit_raw = _amount_from_text(amount_text)
    blob = " ".join([_text(raw.get("raw_title")), _text(raw.get("summary")), _json_text(facts)])
    if event_bucket == "contract_order":
        direction = "positive"
        mechanism_hint = "trend_capex"
        horizon = "medium_term"
    elif event_bucket == "capacity_capex":
        direction = _direction_from_text(blob, positive_words=["扩产", "投产", "建设", "增资", "技改", "复工"], negative_words=["停产", "终止", "延期"], default="positive")
        mechanism_hint = "trend_capex"
        horizon = "medium_term"
    else:
        direction = _direction_from_text(blob, positive_words=["涨价", "提价", "限产", "停产", "检修", "去库"], negative_words=["降价", "复工复产", "复产", "累库"], default="neutral")
        mechanism_hint = "price_inventory"
        horizon = "short_term"
    source_fields = _base_source_fields(raw=raw, source_path=source_path)
    confidence = float(source_fields["source_confidence"])
    if identity.get("symbol"):
        confidence = min(0.98, confidence + 0.05)
    if amount_cny is not None:
        confidence = min(0.98, confidence + 0.04)
    importance_level, is_major = _importance_level(amount_cny=amount_cny, source_confidence=confidence, major_hint=bool(identity.get("symbol") and event_bucket == "contract_order"))
    event_date = _normalize_date(
        facts.get("announcement_date")
        or facts.get("signing_date")
        or facts.get("effective_date")
        or raw.get("publish_time")
        or raw.get("crawl_time")
    )
    trade_date = event_date
    company_action = {
        "fact_id": f"evt::{_text(raw.get('event_id')) or _stable_id(blob)}",
        "source_event_id": _text(raw.get("event_id")),
        "trade_date": trade_date,
        "event_date": event_date,
        "publish_date": _normalize_date(raw.get("publish_time") or raw.get("crawl_time") or event_date),
        "symbol": _text(identity.get("symbol")).upper(),
        "ts_code": _text(identity.get("ts_code")).upper(),
        "company_name": _text(identity.get("company_name")),
        "event_type": event_bucket,
        "event_subtype": _text(raw.get("event_type")),
        "direction": direction,
        "headline": _text(raw.get("raw_title") or raw.get("summary") or facts.get("event_description") or facts.get("project_name"))[:220],
        "raw_source_name": source_fields["raw_source_name"],
        "raw_source_url": source_fields["raw_source_url"],
        "source_class": source_fields["source_class"],
        "source_authority": source_fields["source_authority"],
        "source_confidence": round(confidence, 4),
        "is_issuer_disclosure": source_fields["is_issuer_disclosure"],
        "is_official_source": source_fields["is_official_source"],
        "is_structured_from_text": source_fields["is_structured_from_text"],
        "currency": "CNY" if amount_cny is not None else "",
        "amount_raw": amount_raw,
        "amount_cny": amount_cny if amount_cny is not None else "",
        "quantity_raw": _text(facts.get("quantity") or facts.get("project_count")),
        "unit_raw": unit_raw,
        "counterparty": _text(facts.get("counterparty") or facts.get("client") or facts.get("buyer_name") or facts.get("purchaser") or facts.get("target_company")),
        "counterparty_type": "government" if any(token in blob for token in ["政府", "海关", "中国移动", "采购人"]) else "",
        "project_name": _text(facts.get("project_name") or facts.get("project_type") or facts.get("award_name")),
        "region": _text(facts.get("location") or facts.get("province") or facts.get("region")),
        "industry_chain_hint": _text(facts.get("product_name") or facts.get("focus_areas") or facts.get("purpose")),
        "mechanism_hint": mechanism_hint,
        "impact_horizon": horizon,
        "importance_level": importance_level,
        "is_major_event": is_major,
        "notes": f"event_store_source={source_path}",
        "raw_payload_path": f"{source_path}::{_text(raw.get('event_id'))}",
    }
    contract_row = None
    if event_bucket == "contract_order":
        contract_row = {
            "fact_id": f"contract::{company_action['fact_id']}",
            "source_event_id": company_action["source_event_id"],
            "trade_date": trade_date,
            "event_date": event_date,
            "symbol": company_action["symbol"],
            "company_name": company_action["company_name"],
            "event_type": _text(raw.get("event_type")),
            "contract_type": _text(facts.get("agreement_type") or facts.get("contract_type") or "order_award"),
            "tender_type": "bid_award" if any(token in blob for token in ["中标", "招标", "award"]) else "",
            "project_name": company_action["project_name"],
            "project_owner": _text(facts.get("project_owner") or facts.get("buyer_name") or facts.get("purchaser") or facts.get("client")),
            "counterparty": company_action["counterparty"],
            "counterparty_is_government": 1 if any(token in blob for token in ["政府", "海关", "采购"]) else 0,
            "amount_raw": amount_raw,
            "amount_cny": amount_cny if amount_cny is not None else "",
            "amount_ratio_to_revenue": "",
            "is_framework_agreement": 1 if "框架" in blob else 0,
            "is_binding_contract": 0 if any(token in blob for token in ["意向", "框架"]) else 1,
            "is_bid_award": 1 if any(token in blob for token in ["中标", "招标", "award"]) else 0,
            "is_new_order": 1,
            "is_backlog_related": 1 if "在手订单" in blob else 0,
            "delivery_window": _text(facts.get("delivery_window") or facts.get("delivery_period")),
            "business_segment": _text(facts.get("business_segment") or facts.get("project_type")),
            "mechanism_hint": mechanism_hint,
            "source_name": company_action["raw_source_name"],
            "source_url": company_action["raw_source_url"],
            "source_class": company_action["source_class"],
            "raw_payload_path": company_action["raw_payload_path"],
        }
    supply_row = None
    if event_bucket in {"capacity_capex", "price_supply"}:
        signal_type = "capacity_change"
        if event_bucket == "price_supply":
            if any(token in blob for token in ["涨价", "提价", "降价"]):
                signal_type = "price_change"
            elif any(token in blob for token in ["停产", "限产", "检修", "复工复产", "复产"]):
                signal_type = "supply_disruption"
        supply_row = {
            "fact_id": f"supply::{company_action['fact_id']}",
            "source_event_id": company_action["source_event_id"],
            "trade_date": trade_date,
            "event_date": event_date,
            "symbol": company_action["symbol"],
            "company_name": company_action["company_name"],
            "event_type": _text(raw.get("event_type")),
            "signal_type": signal_type,
            "direction": direction,
            "product_name": _text(facts.get("product_name") or facts.get("commodity") or facts.get("pricing_subject")),
            "industry_name": _text(facts.get("industry_name") or facts.get("industry") or company_action["industry_chain_hint"]),
            "capacity_change_desc": _text(facts.get("capacity_change_desc") or facts.get("purpose") or facts.get("project_type")),
            "price_change_desc": _text(facts.get("action") if signal_type == "price_change" else ""),
            "shutdown_desc": _text((facts.get("shutdown_desc") or facts.get("action")) if signal_type == "supply_disruption" else ""),
            "operation_rate_desc": _text(facts.get("operation_rate_desc") or facts.get("status")),
            "expected_duration": _text(facts.get("expected_duration") or facts.get("delivery_window")),
            "mechanism_hint": mechanism_hint,
            "source_name": company_action["raw_source_name"],
            "source_url": company_action["raw_source_url"],
            "source_class": company_action["source_class"],
            "raw_payload_path": company_action["raw_payload_path"],
        }
    return company_action, contract_row, supply_row


def _recent_payloads(payloads: List[Dict[str, Any]], date_field: str, lookback_days: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now().date() - timedelta(days=max(1, int(lookback_days or 45)))
    out: List[Dict[str, Any]] = []
    for payload in payloads:
        date_text = _normalize_date(payload.get(date_field))
        if not date_text:
            continue
        try:
            current = datetime.strptime(date_text, "%Y-%m-%d").date()
        except Exception:
            continue
        if current >= cutoff:
            out.append(payload)
    return out


def _build_earnings_company_actions(*, affordable_db: Path, by_symbol: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for payload in _recent_payloads(_load_affordable_payloads(affordable_db, "forecast"), "ann_date", 90):
        symbol = _text(payload.get("ts_code")).upper()
        stock = by_symbol.get(symbol, {"symbol": symbol, "ts_code": symbol, "company_name": ""})
        pmax = float(payload.get("p_change_max") or 0.0)
        pmin = float(payload.get("p_change_min") or 0.0)
        direction = "negative" if min(pmax, pmin) < 0 else "positive"
        confidence = 0.86
        importance_level, is_major = _importance_level(
            amount_cny=None,
            source_confidence=confidence + (0.08 if abs(pmax) >= 50 else 0.0),
            major_hint=abs(pmax) >= 80,
        )
        out.append(
            {
                "fact_id": f"forecast::{symbol}::{_text(payload.get('ann_date'))}::{_text(payload.get('type'))}",
                "source_event_id": "",
                "trade_date": _normalize_date(payload.get("ann_date")),
                "event_date": _normalize_date(payload.get("ann_date")),
                "publish_date": _normalize_date(payload.get("ann_date")),
                "symbol": _text(stock.get("symbol")).upper(),
                "ts_code": _text(stock.get("ts_code")).upper(),
                "company_name": _text(stock.get("company_name")),
                "event_type": "earnings_guidance",
                "event_subtype": _text(payload.get("type") or "forecast"),
                "direction": direction,
                "headline": _text(payload.get("summary") or payload.get("change_reason"))[:220],
                "raw_source_name": "tushare.forecast",
                "raw_source_url": "",
                "source_class": "derived_from_truth",
                "source_authority": "issuer_disclosure_mirror",
                "source_confidence": round(confidence, 4),
                "is_issuer_disclosure": 1,
                "is_official_source": 0,
                "is_structured_from_text": 0,
                "currency": "CNY",
                "amount_raw": _text(payload.get("net_profit_max") or payload.get("net_profit_min")),
                "amount_cny": payload.get("net_profit_max") or payload.get("net_profit_min") or "",
                "quantity_raw": "",
                "unit_raw": "CNY",
                "counterparty": "",
                "counterparty_type": "",
                "project_name": "",
                "region": "",
                "industry_chain_hint": "",
                "mechanism_hint": "earnings_validation",
                "impact_horizon": "quarterly",
                "importance_level": importance_level,
                "is_major_event": is_major,
                "notes": _text(payload.get("change_reason"))[:220],
                "raw_payload_path": f"{affordable_db}::forecast::{symbol}",
            }
        )
    for payload in _recent_payloads(_load_affordable_payloads(affordable_db, "express"), "ann_date", 90):
        symbol = _text(payload.get("ts_code")).upper()
        stock = by_symbol.get(symbol, {"symbol": symbol, "ts_code": symbol, "company_name": ""})
        yoy = float(payload.get("yoy_net_profit") or 0.0)
        direction = "positive" if yoy >= 0 else "negative"
        confidence = 0.90
        importance_level, is_major = _importance_level(
            amount_cny=None,
            source_confidence=confidence + (0.06 if abs(yoy) >= 30 else 0.0),
            major_hint=abs(yoy) >= 80,
        )
        out.append(
            {
                "fact_id": f"express::{symbol}::{_text(payload.get('ann_date'))}",
                "source_event_id": "",
                "trade_date": _normalize_date(payload.get("ann_date")),
                "event_date": _normalize_date(payload.get("ann_date")),
                "publish_date": _normalize_date(payload.get("ann_date")),
                "symbol": _text(stock.get("symbol")).upper(),
                "ts_code": _text(stock.get("ts_code")).upper(),
                "company_name": _text(stock.get("company_name")),
                "event_type": "earnings_guidance",
                "event_subtype": "express",
                "direction": direction,
                "headline": _text(payload.get("perf_summary") or "业绩快报")[:220],
                "raw_source_name": "tushare.express",
                "raw_source_url": "",
                "source_class": "derived_from_truth",
                "source_authority": "issuer_disclosure_mirror",
                "source_confidence": round(confidence, 4),
                "is_issuer_disclosure": 1,
                "is_official_source": 0,
                "is_structured_from_text": 0,
                "currency": "CNY",
                "amount_raw": _text(payload.get("n_income")),
                "amount_cny": payload.get("n_income") or "",
                "quantity_raw": "",
                "unit_raw": "CNY",
                "counterparty": "",
                "counterparty_type": "",
                "project_name": "",
                "region": "",
                "industry_chain_hint": "",
                "mechanism_hint": "earnings_validation",
                "impact_horizon": "quarterly",
                "importance_level": importance_level,
                "is_major_event": is_major,
                "notes": f"yoy_net_profit={yoy}",
                "raw_payload_path": f"{affordable_db}::express::{symbol}",
            }
        )
    return out


def _build_ccgp_contract_rows(
    *,
    affordable_db: Path,
    by_symbol: Dict[str, Dict[str, str]],
    by_name: Dict[str, Dict[str, str]],
    by_simple_name: Dict[str, Dict[str, str]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    actions: List[Dict[str, Any]] = []
    contracts: List[Dict[str, Any]] = []
    for payload in _recent_payloads(_load_affordable_payloads(affordable_db, "ccgp_bid_awards"), "published_at", 30):
        title = _text(payload.get("title"))
        purchaser = _text(payload.get("purchaser"))
        identity = _resolve_identity(
            security_code="",
            company_name=purchaser,
            facts={"subject": title},
            by_symbol=by_symbol,
            by_name=by_name,
            by_simple_name=by_simple_name,
        )
        fact_id = f"ccgp::{_text(payload.get('published_at'))}::{_stable_id(title)}"
        actions.append(
            {
                "fact_id": fact_id,
                "source_event_id": "",
                "trade_date": _normalize_date(payload.get("published_at")),
                "event_date": _normalize_date(payload.get("published_at")),
                "publish_date": _normalize_date(payload.get("published_at")),
                "symbol": _text(identity.get("symbol")).upper(),
                "ts_code": _text(identity.get("ts_code")).upper(),
                "company_name": _text(identity.get("company_name")),
                "event_type": "contract_order",
                "event_subtype": "government_bid_award",
                "direction": "positive",
                "headline": title[:220],
                "raw_source_name": "ccgp_bid_awards",
                "raw_source_url": _text(payload.get("source_url")),
                "source_class": "official_truth",
                "source_authority": "ccgp.gov.cn",
                "source_confidence": 0.84 if identity.get("symbol") else 0.68,
                "is_issuer_disclosure": 0,
                "is_official_source": 1,
                "is_structured_from_text": 0,
                "currency": "",
                "amount_raw": "",
                "amount_cny": "",
                "quantity_raw": "",
                "unit_raw": "",
                "counterparty": purchaser,
                "counterparty_type": "government",
                "project_name": title[:180],
                "region": _text(payload.get("region")),
                "industry_chain_hint": "",
                "mechanism_hint": "trend_capex",
                "impact_horizon": "medium_term",
                "importance_level": "medium" if identity.get("symbol") else "low",
                "is_major_event": 0,
                "notes": "ccgp public procurement row; unmapped rows remain research-only and do not enter canonical truth",
                "raw_payload_path": f"{affordable_db}::ccgp_bid_awards::{_stable_id(title)}",
            }
        )
        contracts.append(
            {
                "fact_id": f"contract::{fact_id}",
                "source_event_id": "",
                "trade_date": _normalize_date(payload.get("published_at")),
                "event_date": _normalize_date(payload.get("published_at")),
                "symbol": _text(identity.get("symbol")).upper(),
                "company_name": _text(identity.get("company_name")),
                "event_type": "government_bid_award",
                "contract_type": "government_procurement",
                "tender_type": "bid_award",
                "project_name": title[:180],
                "project_owner": purchaser,
                "counterparty": purchaser,
                "counterparty_is_government": 1,
                "amount_raw": "",
                "amount_cny": "",
                "amount_ratio_to_revenue": "",
                "is_framework_agreement": 0,
                "is_binding_contract": 1,
                "is_bid_award": 1,
                "is_new_order": 1,
                "is_backlog_related": 0,
                "delivery_window": "",
                "business_segment": "",
                "mechanism_hint": "trend_capex",
                "source_name": "ccgp_bid_awards",
                "source_url": _text(payload.get("source_url")),
                "source_class": "official_truth",
                "raw_payload_path": f"{affordable_db}::ccgp_bid_awards::{_stable_id(title)}",
            }
        )
    return actions, contracts


def _load_manual_proxy_rows(config: Dict[str, Any], by_symbol: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    path = resolve_manual_event_proxy_path(config)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            symbol = _normalize_symbol(payload.get("symbol") or payload.get("ts_code"))
            stock = by_symbol.get(symbol, {"symbol": symbol, "ts_code": symbol, "company_name": _text(payload.get("company_name"))})
            rows.append(
                {
                    "fact_id": _text(payload.get("fact_id")) or f"manual::{idx:06d}",
                    "source_event_id": "",
                    "trade_date": _normalize_date(payload.get("trade_date") or payload.get("publish_date")),
                    "event_date": _normalize_date(payload.get("event_date") or payload.get("publish_date")),
                    "publish_date": _normalize_date(payload.get("publish_date") or payload.get("trade_date")),
                    "symbol": _text(stock.get("symbol")).upper(),
                    "ts_code": _text(stock.get("ts_code")).upper(),
                    "company_name": _text(stock.get("company_name")),
                    "event_type": _text(payload.get("event_type") or "manual_proxy_event"),
                    "event_subtype": _text(payload.get("event_subtype")),
                    "direction": _text(payload.get("direction") or "neutral"),
                    "headline": _text(payload.get("headline"))[:220],
                    "raw_source_name": "manual_event_proxy",
                    "raw_source_url": "",
                    "source_class": "research_proxy_manual",
                    "source_authority": "operator_manual_input",
                    "source_confidence": float(payload.get("source_confidence") or 0.55),
                    "is_issuer_disclosure": 0,
                    "is_official_source": 0,
                    "is_structured_from_text": 0,
                    "currency": "",
                    "amount_raw": _text(payload.get("amount_raw")),
                    "amount_cny": payload.get("amount_cny") or "",
                    "quantity_raw": _text(payload.get("quantity_raw")),
                    "unit_raw": _text(payload.get("unit_raw")),
                    "counterparty": _text(payload.get("counterparty")),
                    "counterparty_type": _text(payload.get("counterparty_type")),
                    "project_name": _text(payload.get("project_name")),
                    "region": _text(payload.get("region")),
                    "industry_chain_hint": _text(payload.get("industry_chain_hint")),
                    "mechanism_hint": _text(payload.get("mechanism_hint")),
                    "impact_horizon": _text(payload.get("impact_horizon")),
                    "importance_level": _text(payload.get("importance_level") or "medium"),
                    "is_major_event": int(payload.get("is_major_event") or 0),
                    "notes": _text(payload.get("notes")),
                    "raw_payload_path": f"{path}::{idx}",
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build research-side event fact tables without polluting canonical truth.")
    parser.add_argument("--db-path", default="", help="Override research fact sqlite path.")
    parser.add_argument("--lookback-days", type=int, default=45, help="Look back window for event_store ingestion.")
    args = parser.parse_args()

    config = build_runtime_config()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_started_at = datetime.now()
    affordable_db = Path(str(config.get("paths", {}).get("affordable_sqlite_path", "") or "")).resolve()
    db_path = Path(args.db_path).resolve() if args.db_path else resolve_research_fact_sqlite_path(config)

    by_symbol, by_name, by_simple_name = _load_stock_lookup(affordable_db)
    company_actions: List[Dict[str, Any]] = []
    contract_orders: List[Dict[str, Any]] = []
    supply_signals: List[Dict[str, Any]] = []

    event_store_paths = _candidate_event_store_paths(config)
    event_rows_seen = 0
    for source_path, raw in _iter_event_store_rows(event_store_paths, lookback_days=args.lookback_days):
        event_rows_seen += 1
        company_action, contract_row, supply_row = _build_company_action_from_event(
            raw,
            source_path,
            by_symbol=by_symbol,
            by_name=by_name,
            by_simple_name=by_simple_name,
        )
        if company_action:
            company_actions.append(company_action)
        if contract_row:
            contract_orders.append(contract_row)
        if supply_row:
            supply_signals.append(supply_row)

    company_actions.extend(_build_earnings_company_actions(affordable_db=affordable_db, by_symbol=by_symbol))
    ccgp_actions, ccgp_contracts = _build_ccgp_contract_rows(
        affordable_db=affordable_db,
        by_symbol=by_symbol,
        by_name=by_name,
        by_simple_name=by_simple_name,
    )
    company_actions.extend(ccgp_actions)
    contract_orders.extend(ccgp_contracts)
    company_actions.extend(_load_manual_proxy_rows(config, by_symbol=by_symbol))

    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        register_default_field_lineage(conn)
        action_rows = upsert_rows(conn, "event_fact_company_actions", company_actions, ("fact_id",))
        contract_rows = upsert_rows(conn, "event_fact_contract_orders", contract_orders, ("fact_id",))
        supply_rows = upsert_rows(conn, "event_fact_supply_chain_signals", supply_signals, ("fact_id",))
        insert_source_fetch_logs(
            conn,
            [
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="event_store_curated",
                    source_name="event_store_jsonl",
                    source_url="|".join(str(path) for path in event_store_paths[:2]),
                    started_at=run_started_at,
                    finished_at=datetime.now(),
                    rows_written=int(event_rows_seen),
                    items_seen=int(event_rows_seen),
                    message="event_store curated ingestion",
                ),
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="earnings_mirror",
                    source_name="affordable_sqlite",
                    source_url=str(affordable_db),
                    started_at=run_started_at,
                    finished_at=datetime.now(),
                    rows_written=int(len([row for row in company_actions if _text(row.get("raw_source_name")).startswith("tushare.")])),
                    items_seen=int(len([row for row in company_actions if _text(row.get("raw_source_name")).startswith("tushare.")])),
                    message="forecast/express mirrored into event facts",
                ),
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="ccgp_bid_awards",
                    source_name="ccgp.gov.cn",
                    source_url=str(affordable_db),
                    started_at=run_started_at,
                    finished_at=datetime.now(),
                    rows_written=int(len([row for row in contract_orders if _text(row.get("source_name")) == "ccgp_bid_awards"])),
                    items_seen=int(len([row for row in contract_orders if _text(row.get("source_name")) == "ccgp_bid_awards"])),
                    message="ccgp bid awards mirrored into contract facts",
                ),
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="manual_event_proxy",
                    source_name="manual_event_proxy",
                    source_url=str(resolve_manual_event_proxy_path(config)),
                    started_at=run_started_at,
                    finished_at=datetime.now(),
                    rows_written=int(len([row for row in company_actions if _text(row.get("raw_source_name")) == "manual_event_proxy"])),
                    items_seen=int(len([row for row in company_actions if _text(row.get("raw_source_name")) == "manual_event_proxy"])),
                    message="manual proxy event ingestion",
                ),
            ],
        )
        counts = {
            "event_fact_company_actions": conn.execute("SELECT COUNT(*) FROM event_fact_company_actions").fetchone()[0],
            "event_fact_contract_orders": conn.execute("SELECT COUNT(*) FROM event_fact_contract_orders").fetchone()[0],
            "event_fact_supply_chain_signals": conn.execute("SELECT COUNT(*) FROM event_fact_supply_chain_signals").fetchone()[0],
        }

    summary = {
        "db_path": str(db_path),
        "affordable_db": str(affordable_db),
        "event_store_paths": [str(path) for path in event_store_paths],
        "event_rows_seen": event_rows_seen,
        "rows_upserted": {
            "event_fact_company_actions": action_rows,
            "event_fact_contract_orders": contract_rows,
            "event_fact_supply_chain_signals": supply_rows,
        },
        "table_counts": counts,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
