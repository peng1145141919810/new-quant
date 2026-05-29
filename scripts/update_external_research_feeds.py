from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


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
    resolve_research_fact_sqlite_path,
    sqlite_connection,
    upsert_rows,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> datetime:
    return datetime.now()


def _stable_id(*parts: Any) -> str:
    seed = "||".join(_text(item) for item in parts if _text(item))
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:20]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
        "log_id": f"external::{run_id}::{dataset_name}",
        "run_id": run_id,
        "pipeline_name": "external_research_refresh",
        "dataset_name": dataset_name,
        "source_id": dataset_name,
        "source_name": source_name,
        "source_url": source_url,
        "source_domain": urlparse(source_url).netloc.lower() if source_url else "",
        "trade_date": _now().strftime("%Y-%m-%d"),
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


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_cookie_value(cookie_header_path: str, url: str = "") -> str:
    cookie_path = Path(cookie_header_path).resolve() if _text(cookie_header_path) else None
    if cookie_path is None or not cookie_path.is_file():
        return ""
    try:
        raw_text = cookie_path.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff").strip()
    except Exception:
        return ""
    if not raw_text:
        return ""
    try:
        payload = json.loads(raw_text)
    except Exception:
        return raw_text
    if not isinstance(payload, dict):
        return raw_text
    host = urlparse(url).netloc.lower()
    if host and host in payload:
        return _text(payload.get(host))
    for key in [".qianzhan.com", "qianzhan.com", "default"]:
        if key in payload:
            return _text(payload.get(key))
    return ""


def _headers(cookie_header_path: str = "", url: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    cookie_value = _resolve_cookie_value(cookie_header_path=cookie_header_path, url=url)
    if cookie_value:
        headers["Cookie"] = cookie_value
    return headers


def _fetch_text(url: str, headers: Dict[str, str], timeout_seconds: int) -> str:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=max(int(timeout_seconds or 0), 5)) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read()
    try:
        return body.decode(charset, errors="ignore")
    except Exception:
        return body.decode("utf-8", errors="ignore")


def _clean_html_text(html_text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(html_text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    return _clean_html_text(match.group(1)) if match else ""


def _extract_publish_date(text: str) -> str:
    candidates = re.findall(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", text)
    for item in candidates:
        digits = re.sub(r"\D", "", item)
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _extract_numeric_snippets(text: str, limit: int = 8) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    pattern = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()、/\-]{2,40})[:：]?\s*(-?\d+(?:\.\d+)?)\s*(亿元|亿元人民币|万元|亿元/年|万吨|万台|万件|亿美元|%|个百分点|元/吨|吨|台|件|人|家|个)?")
    for match in pattern.finditer(text):
        indicator_name = _text(match.group(1))
        if len(indicator_name) < 2:
            continue
        raw_num = _text(match.group(2))
        unit = _text(match.group(3))
        try:
            value_num = float(raw_num)
        except Exception:
            value_num = None
        rows.append({
            "indicator_name": indicator_name[:80],
            "value_raw": f"{raw_num}{unit}",
            "value_num": value_num,
            "unit": unit[:20],
        })
        if len(rows) >= max(int(limit or 0), 0):
            break
    return rows


def _extract_links(html_text: str, base_url: str) -> List[str]:
    pattern = re.compile(r'href=["\']([^"\']+)["\']', flags=re.IGNORECASE)
    parsed_base = urlparse(base_url)
    allowed_hosts = {
        "d.qianzhan.com",
        "x.qianzhan.com",
        "stock.qianzhan.com",
        "zc.qianzhan.com",
        "www.qianzhan.com",
    }
    out: List[str] = []
    for raw in pattern.findall(html_text):
        url = urljoin(base_url, raw)
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if parsed.scheme not in {"http", "https"}:
            continue
        if host not in allowed_hosts:
            continue
        if parsed.fragment:
            url = url.split("#", 1)[0]
        if url not in out:
            out.append(url)
    return out


def _is_industry_indicator_stable_url(url: str) -> bool:
    """Heuristic: industry / macro / data / chain pages are more stable for numeric indicator extraction."""
    u = str(url or "").lower()
    needles = (
        "/industry/",
        "/hangye/",
        "industrydata",
        "hydata",
        "/macro",
        "/tmt/",
        "/energy/",
        "/data/",
        "indicator",
        "sector",
        "chain",
        "industry_",
        "_industry",
        "chart",
        "statistics",
        "stats",
    )
    return any(n in u for n in needles)


def _qianzhan_numeric_snippet_limit(url: str, default_limit: int = 6) -> int:
    return 14 if _is_industry_indicator_stable_url(url) else int(default_limit)


def _qianzhan_priority(url: str) -> int:
    text = url.lower()
    score = 0
    if _is_industry_indicator_stable_url(url):
        score += 28
    priority_terms = [
        "industry",
        "detail",
        "chart",
        "data",
        "stock",
        "policy",
        "report",
        "chain",
        "market",
        "import",
        "export",
        "macro",
    ]
    for idx, term in enumerate(priority_terms, start=1):
        if term in text:
            score += 12 - min(idx, 10)
    if any(seg in text for seg in ["/detail/", "/chart/", "/industry/", "/policy/", "/stock/"]):
        score += 10
    if text.count("/") >= 4:
        score += 2
    return score


def _qianzhan_indicator_category(url: str, llm: Dict[str, Any]) -> str:
    if _is_industry_indicator_stable_url(url):
        return "industry_indicator_page"
    return _text(llm.get("card_type") or "knowledge_page")


def _industry_from_text(text: str) -> str:
    rules = [
        ("新能源金属", ["锂", "碳酸锂", "工业硅", "光伏", "硅料", "电池材料"]),
        ("有色", ["铜", "铝", "氧化铝", "铅", "锌", "稀土", "金属"]),
        ("化工", ["甲醇", "纯碱", "烧碱", "MDI", "化工", "苯乙烯", "PTA"]),
        ("电子", ["半导体", "集成电路", "服务器", "芯片", "面板", "电子"]),
        ("机械设备", ["工程机械", "机床", "机器人", "自动化"]),
    ]
    lowered = _text(text)
    for industry_name, keywords in rules:
        if any(keyword in lowered for keyword in keywords):
            return industry_name
    return ""


def _llm_router(config: Dict[str, Any]):
    from engine.llm_router import LLMRouter

    return LLMRouter(
        provider_cfg=dict(config.get("providers", {}) or {}),
        schema_root=None,
        local_ollama_cfg=dict(config.get("local_ollama", {}) or {}),
    )


def _llm_enrich_page(config: Dict[str, Any], title: str, text: str, kind: str) -> Dict[str, Any]:
    cfg = dict(config.get("external_research_refresh", {}) or {})
    if not bool(cfg.get("llm_enrichment_enabled", False)):
        return {}
    provider = _text(cfg.get("llm_provider") or "deepseek_worker").lower()
    timeout_seconds = int(cfg.get("llm_timeout_seconds", 30) or 30)
    snippet = text[:1800]
    try:
        router = _llm_router(config)
    except Exception:
        return {}
    system_prompt = (
        "你是量化研究系统的受限信息抽取器。"
        "只输出 JSON，不要编造事实。"
        "字段只允许：industry_name, topic_name, card_type, mechanism_hint, entity_name, event_type, relevance_score, notes。"
    )
    user_prompt = (
        f"页面类型: {kind}\n"
        f"标题: {title}\n"
        f"正文片段: {snippet}\n"
        "请输出 JSON，relevance_score 取 0 到 1。"
    )
    try:
        if provider == "local_ollama":
            result = router.call_local_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds)
        elif provider == "openai_research":
            result = router.call_research_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds, max_output_tokens=300)
        else:
            result = router.call_worker_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds)
    except Exception:
        return {}
    return dict(result.get("data", {}) or {}) if bool(result.get("ok", False)) else {}


def _load_seed_payload(path: Path, default_payload: Dict[str, Any]) -> Dict[str, Any]:
    if path.exists():
        payload = _load_json(path)
        if payload:
            return payload
    return default_payload


def _default_seed_payload() -> Dict[str, Any]:
    return {
        "qianzhan": [
            "https://d.qianzhan.com/",
            "https://x.qianzhan.com/",
            "https://stock.qianzhan.com/",
            "https://zc.qianzhan.com/",
            "https://www.qianzhan.com/industry/",
            "https://www.qianzhan.com/data/",
            "https://www.qianzhan.com/analyst/industry/",
        ],
        "ggzy": [
            "https://www.ggzy.gov.cn/",
        ],
    }


def _company_lookup(config: Dict[str, Any]) -> Dict[str, str]:
    affordable_db = Path(_text(dict(config.get("paths", {}) or {}).get("affordable_sqlite_path"))).resolve()
    if not affordable_db.exists():
        return {}
    import sqlite3

    conn = sqlite3.connect(str(affordable_db))
    try:
        rows = conn.execute("SELECT payload_json FROM affordable_dataset_rows WHERE dataset = 'stock_basic'").fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    mapping: Dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        ts_code = _text(payload.get("ts_code")).upper()
        name = _text(payload.get("name"))
        if not ts_code or not name:
            continue
        mapping[name] = ts_code
        simple = re.sub(r"(股份有限公司|有限责任公司|有限公司|集团股份公司|股份公司|集团|公司)$", "", name).strip()
        if simple and simple not in mapping:
            mapping[simple] = ts_code
    return mapping


def _extract_entities(text: str, company_lookup: Dict[str, str]) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    for name, ts_code in company_lookup.items():
        if not name or len(name) < 2:
            continue
        if name in text:
            payload = {"entity_name": name, "ts_code": ts_code}
            if payload not in hits:
                hits.append(payload)
        if len(hits) >= 6:
            break
    return hits


def _fetch_qianzhan_rows(config: Dict[str, Any], seed_payload: Dict[str, Any], artifact_root: Path) -> Dict[str, List[Dict[str, Any]]]:
    cfg = dict(config.get("external_research_refresh", {}) or {})
    budget = int(cfg.get("qianzhan_daily_page_budget", 24) or 24)
    timeout_seconds = int(cfg.get("fetch_timeout_seconds", 20) or 20)
    knowledge_rows: List[Dict[str, Any]] = []
    indicator_rows: List[Dict[str, Any]] = []
    page_dir = artifact_root / "qianzhan_pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    queue: List[str] = list(seed_payload.get("qianzhan", []) or [])
    visited: Set[str] = set()
    discovered: Set[str] = set(queue)
    idx = 0
    while queue and idx < max(budget, 0):
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        headers = _headers(_text(cfg.get("qianzhan_cookie_header_path")), url=url)
        try:
            html_text = _fetch_text(url, headers=headers, timeout_seconds=timeout_seconds)
        except Exception:
            continue
        title = _extract_title(html_text)
        text = _clean_html_text(html_text)
        section = urlparse(url).netloc.split(".")[0] or "qianzhan"
        publish_date = _extract_publish_date(text)
        llm = _llm_enrich_page(config=config, title=title, text=text, kind="qianzhan_page")
        industry_name = _text(llm.get("industry_name")) or _industry_from_text(f"{title} {text[:300]}")
        raw_path = page_dir / f"{section}_{idx:03d}.json"
        _write_json(raw_path, {
            "url": url,
            "title": title,
            "publish_date": publish_date,
            "section": section,
            "body_excerpt": text[:4000],
            "llm": llm,
            "discovered_links": [],
        })
        lim = _qianzhan_numeric_snippet_limit(url, default_limit=6)
        knowledge_rows.append({
            "card_id": _stable_id("qianzhan_card", url, publish_date or _now().date().isoformat()),
            "trade_date": _now().strftime("%Y-%m-%d"),
            "publish_date": publish_date,
            "platform_section": section,
            "page_title": title[:200],
            "page_url": url,
            "industry_name": industry_name,
            "summary_text": text[:1000],
            "extracted_numbers": json.dumps(_extract_numeric_snippets(text, limit=lim), ensure_ascii=False),
            "source_class": "mixed_research_row_level",
            "auth_state": "cookie_header" if "Cookie" in headers else "anonymous",
            "llm_tags": json.dumps(llm, ensure_ascii=False),
            "raw_payload_path": str(raw_path),
        })
        ind_cat = _qianzhan_indicator_category(url, llm)
        sub_ind = _industry_from_text(f"{title} {text[:400]}") if ind_cat == "industry_indicator_page" else ""
        for snippet in _extract_numeric_snippets(text, limit=lim):
            indicator_rows.append({
                "row_id": _stable_id("qianzhan_indicator", url, snippet.get("indicator_name"), publish_date or _now().date().isoformat()),
                "trade_date": _now().strftime("%Y-%m-%d"),
                "publish_date": publish_date,
                "platform_section": section,
                "industry_name": industry_name or sub_ind,
                "sub_industry_name": sub_ind,
                "indicator_name": _text(snippet.get("indicator_name")),
                "indicator_category": ind_cat,
                "value_raw": _text(snippet.get("value_raw")),
                "value_num": snippet.get("value_num"),
                "unit": _text(snippet.get("unit")),
                "direction_hint": _text(llm.get("mechanism_hint")),
                "page_title": title[:200],
                "page_url": url,
                "source_class": "mixed_research_row_level",
                "auth_state": "cookie_header" if "Cookie" in headers else "anonymous",
                "llm_relevance_score": llm.get("relevance_score"),
                "llm_tags": json.dumps(llm, ensure_ascii=False),
                "raw_payload_path": str(raw_path),
            })
        links = _extract_links(html_text, url)
        next_links = [link for link in links if link not in visited and link not in discovered]
        next_links = sorted(next_links, key=_qianzhan_priority, reverse=True)
        for link in next_links[:8]:
            discovered.add(link)
            queue.append(link)
        idx += 1
    return {"knowledge_rows": knowledge_rows, "indicator_rows": indicator_rows}


def _extract_ggzy_links(base_url: str, html_text: str) -> List[str]:
    pattern = re.compile(r'href="([^"]+/information/deal/html/[^"]+\.html)"', flags=re.IGNORECASE)
    out: List[str] = []
    for raw in pattern.findall(html_text):
        url = urljoin(base_url, raw)
        if url not in out:
            out.append(url)
    return out


def _extract_ggzy_notice(config: Dict[str, Any], url: str, html_text: str, raw_path: Path, company_lookup: Dict[str, str]) -> Dict[str, Any]:
    text = _clean_html_text(html_text)
    title = _extract_title(html_text) or text[:120]
    publish_date = _extract_publish_date(text)
    parsed = urlparse(url)
    province_match = re.search(r"/([0-9]{6})/", url)
    province = province_match.group(1) if province_match else ""
    project_code_match = re.search(r"(项目编号|采购编号|招标编号|项目编码)[:：]?\s*([A-Za-z0-9\-_]+)", text)
    project_code = _text(project_code_match.group(2) if project_code_match else "")
    amount_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(亿元|万元|元)", text)
    amount_raw = ""
    amount_cny = None
    if amount_match:
        amount_raw = f"{amount_match.group(1)}{amount_match.group(2)}"
        factor = {"亿元": 1e8, "万元": 1e4, "元": 1.0}.get(amount_match.group(2), 1.0)
        amount_cny = round(float(amount_match.group(1)) * factor, 2)
    llm = _llm_enrich_page(config=config, title=title, text=text, kind="ggzy_notice")
    entities = _extract_entities(f"{title} {text[:4000]}", company_lookup)
    return {
        "notice_id": _stable_id("ggzy_notice", url, publish_date or _now().date().isoformat()),
        "trade_date": _now().strftime("%Y-%m-%d"),
        "publish_date": publish_date,
        "notice_type": "result_notice" if ("中标" in title or "成交" in title) else "notice",
        "business_type": _text(llm.get("event_type")) or ("result_notice" if ("中标" in title or "成交" in title) else "notice"),
        "project_code": project_code,
        "title": title[:300],
        "province": province,
        "source_platform": parsed.netloc,
        "detail_url": url,
        "company_candidates": json.dumps(entities, ensure_ascii=False),
        "amount_raw": amount_raw,
        "amount_cny": amount_cny,
        "llm_event_type": _text(llm.get("event_type")),
        "llm_mechanism_hint": _text(llm.get("mechanism_hint")),
        "llm_relevance_score": llm.get("relevance_score"),
        "llm_candidate_symbols": json.dumps([item.get("ts_code") for item in entities if _text(item.get("ts_code"))], ensure_ascii=False),
        "source_class": "mixed_research_row_level",
        "raw_payload_path": str(raw_path),
    }


def _fetch_ggzy_rows(config: Dict[str, Any], seed_payload: Dict[str, Any], artifact_root: Path) -> Dict[str, List[Dict[str, Any]]]:
    cfg = dict(config.get("external_research_refresh", {}) or {})
    notice_limit = int(cfg.get("ggzy_max_notices_per_run", 36) or 36)
    timeout_seconds = int(cfg.get("fetch_timeout_seconds", 20) or 20)
    headers = _headers()
    raw_dir = artifact_root / "ggzy_notices"
    raw_dir.mkdir(parents=True, exist_ok=True)
    company_lookup = _company_lookup(config)
    notices: List[Dict[str, Any]] = []
    contract_events: List[Dict[str, Any]] = []
    links: List[str] = []
    for seed in list(seed_payload.get("ggzy", []) or []):
        try:
            html_text = _fetch_text(seed, headers=headers, timeout_seconds=timeout_seconds)
        except Exception:
            continue
        for link in _extract_ggzy_links(seed, html_text):
            if link not in links:
                links.append(link)
    for idx, url in enumerate(links[: max(notice_limit, 0)]):
        try:
            html_text = _fetch_text(url, headers=headers, timeout_seconds=timeout_seconds)
        except Exception:
            continue
        raw_path = raw_dir / f"ggzy_notice_{idx:03d}.json"
        _write_json(raw_path, {"url": url, "title": _extract_title(html_text), "body_excerpt": _clean_html_text(html_text)[:4000]})
        row = _extract_ggzy_notice(config=config, url=url, html_text=html_text, raw_path=raw_path, company_lookup=company_lookup)
        notices.append(row)
        company_candidates: List[Dict[str, Any]] = []
        try:
            company_candidates = json.loads(_text(row.get("company_candidates")))
        except Exception:
            company_candidates = []
        listed_hit = next((item for item in company_candidates if _text(item.get("ts_code"))), {})
        entity_name = _text(listed_hit.get("entity_name"))
        ts_code = _text(listed_hit.get("ts_code"))
        if ts_code:
            contract_events.append({
                "fact_id": _stable_id("ggzy_contract_event", row.get("notice_id"), ts_code),
                "source_event_id": _text(row.get("notice_id")),
                "trade_date": _text(row.get("trade_date")),
                "event_date": _text(row.get("publish_date")),
                "symbol": ts_code,
                "company_name": entity_name,
                "event_type": "public_resource_result_notice",
                "contract_type": _text(row.get("notice_type")),
                "tender_type": _text(row.get("business_type")),
                "project_name": _text(row.get("title")),
                "project_owner": "",
                "counterparty": "",
                "counterparty_is_government": 1,
                "amount_raw": _text(row.get("amount_raw")),
                "amount_cny": row.get("amount_cny"),
                "amount_ratio_to_revenue": None,
                "is_framework_agreement": 0,
                "is_binding_contract": 0,
                "is_bid_award": 1,
                "is_new_order": 1,
                "is_backlog_related": 0,
                "delivery_window": "",
                "business_segment": "",
                "mechanism_hint": "public_order",
                "source_name": "ggzy.gov.cn",
                "source_url": _text(row.get("detail_url")),
                "source_class": "mixed_research_row_level",
                "raw_payload_path": _text(row.get("raw_payload_path")),
            })
    return {"notice_rows": notices, "contract_event_rows": contract_events}


def run_external_research_refresh(config: Dict[str, Any]) -> Dict[str, Any]:
    refresh_cfg = dict(config.get("external_research_refresh", {}) or {})
    root = Path(_text(refresh_cfg.get("artifact_root")) or (_repo_root() / "data" / "external_research_feeds" / "latest")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seed_path = Path(_text(refresh_cfg.get("seed_path"))).resolve() if _text(refresh_cfg.get("seed_path")) else Path()
    seed_payload = _load_seed_payload(seed_path, _default_seed_payload())
    run_id = _now().strftime("%Y%m%d_%H%M%S")
    qianzhan_started_at = _now()
    qianzhan_result = _fetch_qianzhan_rows(config=config, seed_payload=seed_payload, artifact_root=root) if bool(refresh_cfg.get("qianzhan_enabled", True)) else {"knowledge_rows": [], "indicator_rows": []}
    qianzhan_finished_at = _now()
    ggzy_started_at = _now()
    ggzy_result = _fetch_ggzy_rows(config=config, seed_payload=seed_payload, artifact_root=root) if bool(refresh_cfg.get("ggzy_enabled", True)) else {"notice_rows": [], "contract_event_rows": []}
    ggzy_finished_at = _now()
    db_path = resolve_research_fact_sqlite_path(config)
    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        register_default_field_lineage(conn)
        if qianzhan_result["knowledge_rows"]:
            upsert_rows(conn, "qianzhan_knowledge_cards", qianzhan_result["knowledge_rows"], key_columns=("card_id",))
        if qianzhan_result["indicator_rows"]:
            upsert_rows(conn, "qianzhan_indicator_daily", qianzhan_result["indicator_rows"], key_columns=("row_id",))
        if ggzy_result["notice_rows"]:
            upsert_rows(conn, "ggzy_notice_index", ggzy_result["notice_rows"], key_columns=("notice_id",))
        if ggzy_result["contract_event_rows"]:
            upsert_rows(conn, "event_fact_contract_orders", ggzy_result["contract_event_rows"], key_columns=("fact_id",))
        insert_source_fetch_logs(
            conn,
            [
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="qianzhan",
                    source_name="qianzhan.com",
                    source_url=str((list(seed_payload.get("qianzhan_seed_urls", []) or [""])[:1] or [""])[0]),
                    started_at=qianzhan_started_at,
                    finished_at=qianzhan_finished_at,
                    rows_written=int(len(qianzhan_result["knowledge_rows"]) + len(qianzhan_result["indicator_rows"])),
                    items_seen=int(len(qianzhan_result["knowledge_rows"])),
                    message="qianzhan external refresh",
                ),
                _build_fetch_log(
                    run_id=run_id,
                    dataset_name="ggzy",
                    source_name="ggzy.gov.cn",
                    source_url=str((list(seed_payload.get("ggzy_seed_urls", []) or [""])[:1] or [""])[0]),
                    started_at=ggzy_started_at,
                    finished_at=ggzy_finished_at,
                    rows_written=int(len(ggzy_result["notice_rows"]) + len(ggzy_result["contract_event_rows"])),
                    items_seen=int(len(ggzy_result["notice_rows"])),
                    message="ggzy external refresh",
                ),
            ],
        )
    manifest = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "trade_date": _now().strftime("%Y-%m-%d"),
        "db_path": str(db_path),
        "qianzhan_pages": int(len(qianzhan_result["knowledge_rows"])),
        "qianzhan_indicator_rows": int(len(qianzhan_result["indicator_rows"])),
        "ggzy_notice_rows": int(len(ggzy_result["notice_rows"])),
        "ggzy_contract_event_rows": int(len(ggzy_result["contract_event_rows"])),
        "artifact_root": str(root),
    }
    _write_json(root / "external_research_refresh_manifest.json", manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Qianzhan/GGZY external research feeds into local SQL.")
    parser.add_argument("--db-path", default="", help="Optional explicit research SQL path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_runtime_config()
    if _text(args.db_path):
        config.setdefault("paths", {})["research_fact_sqlite_path"] = str(Path(args.db_path).resolve())
        config.setdefault("research_fact_refresh", {})["sqlite_path"] = str(Path(args.db_path).resolve())
    result = run_external_research_refresh(config=config)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
