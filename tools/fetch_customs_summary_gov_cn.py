from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.request import Request, urlopen

try:
    import requests
except Exception:
    requests = None


DEFAULT_URLS = [
    "https://www.gov.cn/lianbo/bumen/202508/content_7035523.htm",
    "https://www.gov.cn/lianbo/fabu/202507/content_7031904.htm",
]


@dataclass
class _FallbackResponse:
    content: bytes
    status_code: int
    encoding: str = ""
    apparent_encoding: str = ""

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="ignore")


def _http_get(url: str) -> Any:
    if requests is not None:
        return requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        content = resp.read()
        charset = ""
        try:
            charset = resp.headers.get_content_charset() or ""
        except Exception:
            charset = ""
        return _FallbackResponse(content=content, status_code=int(getattr(resp, "status", 200) or 200), encoding=charset)


def _to_utf8_text(resp: Any) -> str:
    for encoding in ["utf-8", getattr(resp, "apparent_encoding", "") or "", getattr(resp, "encoding", "") or ""]:
        if not encoding:
            continue
        try:
            return resp.content.decode(encoding, errors="ignore")
        except Exception:
            continue
    return resp.text or ""


def _extract_one(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _extract_summary_fields(text: str) -> Dict[str, Any]:
    compact = re.sub(r"\s+", "", text)
    fields: Dict[str, Any] = {
        "period_label": "",
        "period_type": "",
        "total_trade_value": "",
        "total_trade_unit": "",
        "yoy_total_pct": "",
        "export_value": "",
        "yoy_export_pct": "",
        "import_value": "",
        "yoy_import_pct": "",
    }

    period_label = _extract_one(r"(前\d+个月|[1-9]\d*月当月|上半年|一季度|前三季度|全年)", compact)
    fields["period_label"] = period_label
    if "当月" in period_label:
        fields["period_type"] = "monthly"
    elif "前" in period_label or period_label in {"上半年", "一季度", "前三季度", "全年"}:
        fields["period_type"] = "cumulative"

    sentence_source = compact
    if period_label:
        sentences = re.split(r"[。；]", compact)
        preferred = [item for item in sentences if period_label in item and "进出口总值" in item]
        if preferred:
            sentence_source = preferred[0]

    total_match = re.search(
        r"(?:进出口总值|货物贸易进出口总值)([\d\.]+)(万亿元|亿元)[^。；，]*?(?:同比(?:增长|下降)([\-\d\.]+)%)?",
        sentence_source,
    )
    if total_match:
        fields["total_trade_value"] = total_match.group(1)
        fields["total_trade_unit"] = total_match.group(2)
        fields["yoy_total_pct"] = total_match.group(3) or ""

    export_match = re.search(r"出口([\d\.]+)(万亿元|亿元)[^。；，]*?(?:增长|下降)([\-\d\.]+)%", sentence_source)
    if export_match:
        fields["export_value"] = export_match.group(1)
        fields["yoy_export_pct"] = export_match.group(3)

    import_match = re.search(r"进口([\d\.]+)(万亿元|亿元)[^。；，]*?(?:增长|下降)([\-\d\.]+)%", sentence_source)
    if import_match:
        fields["import_value"] = import_match.group(1)
        fields["yoy_import_pct"] = import_match.group(3)

    return fields


def fetch_one(url: str) -> Dict[str, Any]:
    resp = _http_get(url)
    text = _to_utf8_text(resp)
    title = _extract_one(r"<title>(.*?)</title>", text)
    description = _extract_one(r'<meta name="description" content=[\'"](.*?)[\'"]', text)
    release_date = _extract_one(r'<meta name="firstpublishedtime" content=[\'"](\d{4}-\d{2}-\d{2})', text)
    body_text = re.sub(r"<[^>]+>", " ", text)
    body_text = re.sub(r"\s+", " ", body_text)
    extracted = _extract_summary_fields((description or "") + " " + body_text)
    return {
        "source_url": url,
        "status_code": resp.status_code,
        "title": title,
        "description": description,
        "release_date": release_date,
        "fields": extracted,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch official customs summary fields from gov.cn release pages.")
    parser.add_argument("--url", action="append", default=[], help="gov.cn release URL; may be specified multiple times.")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = parse_args()
    urls = [item.strip() for item in args.url if str(item).strip()] or list(DEFAULT_URLS)
    report: List[Dict[str, Any]] = []
    for url in urls:
        report.append(fetch_one(url))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
