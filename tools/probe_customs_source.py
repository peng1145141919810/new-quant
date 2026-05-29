from __future__ import annotations

import json
import sys
from typing import Any, Dict, List

import requests


def _request_probe(url: str) -> Dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, timeout=25, headers=headers, verify=False, allow_redirects=True)
        body = resp.text or ""
        return {
            "url": url,
            "ok": resp.ok,
            "status_code": resp.status_code,
            "final_url": resp.url,
            "content_type": resp.headers.get("content-type", ""),
            "body_head": body[:400],
            "anti_bot_hint": ("$_ts" in body) or ("__jsluid" in body) or ("412" in body[:200]),
        }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "status_code": None,
            "final_url": "",
            "content_type": "",
            "body_head": "",
            "anti_bot_hint": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _selenium_probe(url: str) -> Dict[str, Any]:
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
    except Exception as exc:
        return {"url": url, "available": False, "error": f"{type(exc).__name__}: {exc}"}

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,1200")
    opts.binary_location = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    driver = webdriver.Edge(options=opts)
    try:
        driver.set_page_load_timeout(60)
        driver.get(url)
        page_source = driver.page_source or ""
        return {
            "url": url,
            "available": True,
            "current_url": driver.current_url,
            "title": driver.title,
            "page_source_len": len(page_source),
            "cookies": driver.get_cookies(),
            "body_head": page_source[:400],
        }
    finally:
        driver.quit()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    report: Dict[str, Any] = {
        "official_source_truth": {
            "customs_stats_online_query": "http://stats.customs.gov.cn/",
            "customs_portal": "https://online.customs.gov.cn/",
            "official_cross_reference": "https://www.stats.gov.cn/zs/tjws/zytjzbqs/zckze/202501/t20250121_1958385.html",
        },
        "request_probes": [],
        "selenium_probes": [],
        "diagnosis": [],
    }
    request_urls = [
        "http://stats.customs.gov.cn/",
        "https://stats.customs.gov.cn/",
        "http://stats.customs.gov.cn/indexQuery",
        "https://online.customs.gov.cn/",
    ]
    for url in request_urls:
        report["request_probes"].append(_request_probe(url))

    for url in ["http://stats.customs.gov.cn/", "https://online.customs.gov.cn/"]:
        report["selenium_probes"].append(_selenium_probe(url))

    report["diagnosis"] = [
        "stats.customs.gov.cn is the official free query endpoint according to the NBS methodology note.",
        "In this environment, direct HTTP clients trigger anti-bot or gateway responses on stats.customs.gov.cn.",
        "online.customs.gov.cn is reachable here, but it is not by itself proof that the stats query endpoint is programmatically accessible.",
        "Do not treat the customs dataset as automatically ingestible until a stable browser-driven or export-driven workflow is validated.",
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
