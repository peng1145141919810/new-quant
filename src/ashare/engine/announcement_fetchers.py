# -*- coding: utf-8 -*-
"""免费公告抓取器：巨潮为主，上交所/深交所兜底。"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from bs4 import BeautifulSoup

from .pdf_utils import download_pdf, extract_pdf_text

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _clean_html_text(text: Any) -> str:
    """清洗 HTML 文本。

    Args:
        text: 原文本。

    Returns:
        str: 纯文本。
    """
    s = html.unescape(str(text or ""))
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _make_pdf_path(root: Path, source_name: str, publish_date: str, url: str) -> Path:
    """构造 PDF 缓存路径。

    Args:
        root: 根目录。
        source_name: 来源。
        publish_date: 发布日期。
        url: 链接。

    Returns:
        Path: 路径。
    """
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    date_key = re.sub(r"[^0-9]", "", publish_date)[:8] or datetime.now().strftime("%Y%m%d")
    return root / source_name / date_key / f"{digest}.pdf"


def _is_high_value(title: str, keywords: Iterable[str]) -> bool:
    """判断高价值公告。

    Args:
        title: 标题。
        keywords: 关键词。

    Returns:
        bool: 是否高价值。
    """
    t = str(title or "")
    return any(k in t for k in keywords)


def _format_cninfo_pdf_url(adjunct_url: str) -> str:
    """补全巨潮 PDF 链接。

    Args:
        adjunct_url: 相对链接。

    Returns:
        str: 绝对链接。
    """
    if not adjunct_url:
        return ""
    url = adjunct_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://static.cninfo.com.cn/" + url.lstrip("/")


def fetch_cninfo_announcements(lookback_hours: int, max_pages_per_market: int) -> List[Dict[str, Any]]:
    """抓取巨潮公告目录。

    Args:
        lookback_hours: 回看小时数。
        max_pages_per_market: 每个市场最大页数。

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(hours=lookback_hours)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Referer": "https://www.cninfo.com.cn/",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
    )
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    markets = [("szse", "sz"), ("sse", "sh"), ("bjse", "bj")]
    rows: List[Dict[str, Any]] = []
    se_date = f"{start_dt.strftime('%Y-%m-%d')}~{end_dt.strftime('%Y-%m-%d')}"
    for column, plate in markets:
        for page_num in range(1, max_pages_per_market + 1):
            payload = {
                "pageNum": page_num,
                "pageSize": 50,
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": se_date,
                "sortName": "time",
                "sortType": "desc",
                "isHLtitle": "true",
            }
            try:
                resp = session.post(url, data=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                break
            anns = data.get("announcements") or []
            if not anns:
                break
            for ann in anns:
                title = _clean_html_text(ann.get("announcementTitle") or ann.get("announcementtitle") or "")
                sec_code = str(ann.get("secCode") or ann.get("secCodeFull") or "").strip()
                sec_name = str(ann.get("secName") or "").strip()
                ann_time = ann.get("announcementTime")
                publish_time = ""
                if ann_time:
                    try:
                        publish_time = datetime.fromtimestamp(int(ann_time) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        publish_time = str(ann_time)
                pdf_url = _format_cninfo_pdf_url(str(ann.get("adjunctUrl") or ""))
                category = str(ann.get("announcementTypeName") or ann.get("announcementType") or "").strip()
                rows.append(
                    {
                        "source_type": "announcement",
                        "source_name": f"cninfo_{column}",
                        "publish_time": publish_time,
                        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "title": title,
                        "content": "",
                        "url": pdf_url,
                        "security_code_hint": sec_code or None,
                        "company_name_hint": sec_name or None,
                        "announcement_category": category or None,
                        "market_hint": plate,
                    }
                )
    return rows


def _parse_links_from_html(base_url: str, html_text: str, source_name: str) -> List[Dict[str, Any]]:
    """从 HTML 页面兜底抽取公告链接。

    Args:
        base_url: 基础 URL。
        html_text: HTML 文本。
        source_name: 来源名。

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows: List[Dict[str, Any]] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        title = _clean_html_text(a.get_text(" ", strip=True))
        if not href or not title:
            continue
        if not any(k in href.lower() for k in ["announcement", "bulletin", "notice", ".pdf"]):
            if not any(k in title for k in ["公告", "报告", "说明书", "问询", "回购", "减持", "增持"]):
                continue
        full_url = requests.compat.urljoin(base_url, href)
        key = (full_url, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "source_type": "announcement",
                "source_name": source_name,
                "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title,
                "content": "",
                "url": full_url,
                "security_code_hint": None,
                "company_name_hint": None,
                "announcement_category": None,
                "market_hint": None,
            }
        )
    return rows


def fetch_sse_latest_announcements() -> List[Dict[str, Any]]:
    """抓取上交所最新公告页面并兜底解析。

    Args:
        None

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    url = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        return _parse_links_from_html(url, resp.text, "sse_latest_html")
    except Exception:
        return []


def fetch_szse_latest_announcements() -> List[Dict[str, Any]]:
    """抓取深交所公告页面并兜底解析。

    Args:
        None

    Returns:
        List[Dict[str, Any]]: 事件列表。
    """
    url = "https://www.szse.cn/disclosure/listed/notice/"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        return _parse_links_from_html(url, resp.text, "szse_notice_html")
    except Exception:
        return []


def enrich_announcement_text(
    raw_items: List[Dict[str, Any]],
    pdf_root: Path,
    max_pdf_fetch_per_run: int,
    high_value_title_keywords: Iterable[str],
    download_high_value_pdf: bool,
) -> List[Dict[str, Any]]:
    """为高价值公告补 PDF 正文。

    Args:
        raw_items: 公告列表。
        pdf_root: PDF 缓存根目录。
        max_pdf_fetch_per_run: 最大下载数。
        high_value_title_keywords: 高价值关键词。
        download_high_value_pdf: 是否下载。

    Returns:
        List[Dict[str, Any]]: 补充正文后的列表。
    """
    if not download_high_value_pdf:
        return raw_items
    used = 0
    out: List[Dict[str, Any]] = []
    for item in raw_items:
        row = dict(item)
        title = str(row.get("title", "") or "")
        url = str(row.get("url", "") or "")
        if used < max_pdf_fetch_per_run and url.lower().endswith(".pdf") and _is_high_value(title, high_value_title_keywords):
            pdf_path = _make_pdf_path(pdf_root, str(row.get("source_name", "unknown")), str(row.get("publish_time", "")), url)
            if pdf_path.exists() or download_pdf(url=url, out_path=pdf_path):
                text = extract_pdf_text(pdf_path=pdf_path, max_chars=40000)
                if text:
                    row["content"] = text
                    row["pdf_local_path"] = str(pdf_path)
                used += 1
        out.append(row)
    return out
