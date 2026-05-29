from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pandas as pd
import requests

try:
    from .config_builder import build_runtime_config
    from .config_utils import ensure_dir
    from .llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from engine.config_builder import build_runtime_config
    from engine.config_utils import ensure_dir
    from engine.llm_router import DeepSeekChatClient, LocalOllamaChatClient, OpenAIResponsesClient


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip <= 0:
            text = " ".join(str(data or "").split())
            if text:
                self.parts.append(text)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _symbol(value: Any) -> str:
    text = _text(value).upper()
    if "." in text:
        code, suffix = text.split(".", 1)
        return f"{code.zfill(6)}.{suffix}"
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _source_id(url: str) -> str:
    return hashlib.sha1(str(url or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def _strip_html(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        pass
    return html.unescape(" ".join(parser.parts))


def _clean_url(url: str) -> str:
    text = html.unescape(_text(url))
    if text.startswith("//"):
        return "https:" + text
    parsed = urlparse(text)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return text


def _allowed_domain(url: str, allow_domains: Iterable[str]) -> bool:
    domains = [str(item or "").strip().lower() for item in allow_domains if str(item or "").strip()]
    if not domains:
        return True
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _search_duckduckgo(query: str, *, max_results: int, timeout_seconds: int, allow_domains: List[str]) -> List[Dict[str, Any]]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {"User-Agent": "Mozilla/5.0 evidence-audit/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=max(timeout_seconds, 3))
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []
    links: List[Dict[str, Any]] = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for href, title_html in pattern.findall(text):
        clean = _clean_url(href)
        if not clean.startswith(("http://", "https://")):
            continue
        if not _allowed_domain(clean, allow_domains):
            continue
        title = _strip_html(title_html)
        if any(item["url"] == clean for item in links):
            continue
        links.append({"url": clean, "title": title, "query": query, "source": "duckduckgo_html"})
        if len(links) >= max_results:
            break
    return links


def _fetch_page(url: str, *, timeout_seconds: int, max_chars: int) -> Dict[str, Any]:
    headers = {"User-Agent": "Mozilla/5.0 evidence-audit/1.0"}
    started = time.time()
    try:
        resp = requests.get(url, headers=headers, timeout=max(timeout_seconds, 3))
        status = int(resp.status_code)
        raw = resp.text if resp.text else ""
        extracted = _strip_html(raw)
        return {
            "ok": bool(resp.ok) and bool(extracted),
            "status_code": status,
            "elapsed_seconds": round(time.time() - started, 3),
            "text": extracted[:max_chars],
            "content_hash": hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest(),
            "error": "" if resp.ok else f"http_status_{status}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "elapsed_seconds": round(time.time() - started, 3),
            "text": "",
            "content_hash": "",
            "error": str(exc),
        }


def _candidate_rows(path: Path, limit: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    frame = pd.read_csv(path).fillna("")
    if frame.empty:
        return []
    sort_cols = [col for col in ["alpha_activation_priority", "selection_score", "portfolio_weight", "pred_score_norm"] if col in frame.columns]
    if sort_cols:
        for col in sort_cols:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
        frame = frame.sort_values(sort_cols, ascending=[False] * len(sort_cols))
    rows: List[Dict[str, Any]] = []
    for _, row in frame.head(max(int(limit or 1), 1)).iterrows():
        item = row.to_dict()
        symbol = _symbol(item.get("ts_code") or item.get("code"))
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": _text(item.get("name") or item.get("stock_name")),
                "industry": _text(item.get("industry") or item.get("industry_name")),
                "activation_alpha_family": _text(item.get("activation_alpha_family")),
                "selection_score": round(_float(item.get("selection_score")), 6),
                "alpha_activation_priority": round(_float(item.get("alpha_activation_priority")), 6),
                "candidate_tier": _text(item.get("candidate_tier")),
            }
        )
    return rows


def _build_queries(candidate: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    symbol = _text(candidate.get("symbol"))
    code = symbol.split(".", 1)[0]
    name = _text(candidate.get("name"))
    base = name or code
    templates = list(cfg.get("query_templates", []) or [])
    if not templates:
        templates = [
            "{base} {code} 公告 业绩预告 业绩快报 利润增长",
            "{base} {code} 年报 季报 营收 净利润 现金流 毛利率",
            "{base} {code} 中标 合同 订单 客户 供货",
            "{base} {code} 减持 质押 冻结 立案 调查 处罚 诉讼",
            "{base} {code} 退市风险 ST 非标审计 业绩亏损",
            "{base} {code} 涨价 产能 扩产 停产 产品价格",
        ]
    out: List[str] = []
    for template in templates:
        text = str(template).format(base=base, code=code, symbol=symbol, industry=_text(candidate.get("industry")))
        if text and text not in out:
            out.append(text)
    return out


def _default_review(reason: str, candidate: Dict[str, Any], sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "ok": False,
        "symbol": _text(candidate.get("symbol")),
        "evidence_grade": "C" if sources else "D",
        "verdict": "watch_only",
        "positive_claims": [],
        "negative_claims": [],
        "uncertainty_flags": [reason],
        "source_ids": [str(item.get("source_id")) for item in sources],
        "review_summary": reason,
    }


def _llm_review_candidate(config: Dict[str, Any], cfg: Dict[str, Any], candidate: Dict[str, Any], sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    provider = _text(cfg.get("llm_provider") or "deepseek_worker") or "deepseek_worker"
    timeout_seconds = int(cfg.get("llm_timeout_seconds", 60) or 60)
    max_sources = max(int(cfg.get("llm_max_sources", 8) or 8), 1)
    source_payload = [
        {
            "source_id": item.get("source_id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "query": item.get("query"),
            "text_excerpt": _text(item.get("text"))[: int(cfg.get("llm_source_excerpt_chars", 2600) or 2600)],
        }
        for item in sources[:max_sources]
    ]
    if not source_payload:
        return _default_review("no_sources_found", candidate, sources)
    system_prompt = (
        "You are an evidence auditor for an A-share quant system. "
        "You must not invent facts. Every positive or negative claim must cite source_ids from the input. "
        "If the sources do not support a conclusion, say no evidence. Return JSON only."
    )
    payload = {
        "task": "Audit whether non-structured public evidence supports or contradicts this hard-data selected candidate.",
        "candidate": candidate,
        "sources": source_payload,
        "required_json_schema": {
            "symbol": "string",
            "evidence_grade": "A|B|C|D|F",
            "verdict": "support|weak_support|watch_only|avoid|veto",
            "positive_claims": [{"claim": "string", "source_ids": ["string"]}],
            "negative_claims": [{"claim": "string", "source_ids": ["string"]}],
            "uncertainty_flags": ["string"],
            "review_summary": "string",
        },
        "grading_rules": [
            "A requires strong hard-data candidate plus clear recent positive source evidence and no material negative evidence.",
            "B requires at least one relevant positive source and no severe negative evidence.",
            "C means hard-data candidate but sources are weak or absent.",
            "D means sources are confusing, stale, or not enough to support a view.",
            "F means material negative evidence or source evidence contradicts the candidate.",
            "Never upgrade based on speculation or industry common sense without source_id support.",
        ],
    }
    providers_cfg = dict(config.get("providers", {}) or {})
    if provider == "local_ollama":
        client = LocalOllamaChatClient(provider_name="evidence_audit_ollama", cfg=dict(config.get("local_ollama", {}) or {}))
        result = client.chat_json_detailed(system_prompt, json.dumps(payload, ensure_ascii=False), timeout_seconds=timeout_seconds)
    elif provider == "openai_research":
        client = OpenAIResponsesClient(provider_name="evidence_audit_openai", cfg=dict(providers_cfg.get("openai_research", {}) or {}))
        result = client.create_json_response_detailed(system_prompt, json.dumps(payload, ensure_ascii=False), timeout_seconds=timeout_seconds)
    else:
        client = DeepSeekChatClient(provider_name="evidence_audit_deepseek", cfg=dict(providers_cfg.get("deepseek_worker", {}) or {}))
        result = client.chat_json_detailed(system_prompt, json.dumps(payload, ensure_ascii=False), timeout_seconds=timeout_seconds)
    if not bool(result.get("ok", False)):
        return _default_review(_text(result.get("error_message")) or "llm_review_failed", candidate, sources)
    raw = dict(result.get("data", {}) or {})
    grade = _text(raw.get("evidence_grade")).upper()
    if grade not in {"A", "B", "C", "D", "F"}:
        grade = "D"
    allowed_source_ids = {str(item.get("source_id")) for item in sources}

    def _claims(key: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in list(raw.get(key, []) or []):
            row = dict(item or {})
            ids = [str(x) for x in list(row.get("source_ids", []) or []) if str(x) in allowed_source_ids]
            claim = _text(row.get("claim"))
            if claim and ids:
                out.append({"claim": claim[:500], "source_ids": ids[:6]})
        return out

    return {
        "ok": True,
        "symbol": _text(candidate.get("symbol")),
        "evidence_grade": grade,
        "verdict": _text(raw.get("verdict")) or ("veto" if grade == "F" else "watch_only"),
        "positive_claims": _claims("positive_claims"),
        "negative_claims": _claims("negative_claims"),
        "uncertainty_flags": [_text(x) for x in list(raw.get("uncertainty_flags", []) or []) if _text(x)][:8],
        "source_ids": sorted(allowed_source_ids),
        "review_summary": _text(raw.get("review_summary"))[:1000],
        "provider": provider,
        "model": _text(result.get("model")),
    }


def run_evidence_audit(config: Dict[str, Any], *, candidate_pool_path: Path | None = None, limit: int | None = None) -> Dict[str, Any]:
    cfg = dict(config.get("evidence_audit", {}) or {})
    if not bool(cfg.get("enabled", True)):
        return {"enabled": False, "ok": True, "ran": False, "message": "disabled"}
    paths = dict(config.get("paths", {}) or {})
    out_root = ensure_dir(Path(_text(cfg.get("artifact_root")) or Path(paths.get("portfolio_output_root", "data/portfolio_recommendation_v6")) / "evidence_audit_v1").resolve())
    pool_path = Path(candidate_pool_path or _text(cfg.get("candidate_pool_path")) or Path(paths.get("portfolio_output_root", "data/portfolio_recommendation_v6")) / "candidate_pool.csv").resolve()
    max_candidates = int(limit or cfg.get("max_candidates", 40) or 40)
    max_results_per_query = int(cfg.get("max_results_per_query", 3) or 3)
    max_sources_per_symbol = int(cfg.get("max_sources_per_symbol", 8) or 8)
    timeout_seconds = int(cfg.get("fetch_timeout_seconds", 12) or 12)
    max_page_chars = int(cfg.get("max_page_chars", 8000) or 8000)
    allow_domains = list(cfg.get("allow_domains", []) or [])
    candidates = _candidate_rows(pool_path, max_candidates)
    audit_rows: List[Dict[str, Any]] = []
    source_rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        links: List[Dict[str, Any]] = []
        for query in _build_queries(candidate, cfg):
            links.extend(
                _search_duckduckgo(
                    query,
                    max_results=max_results_per_query,
                    timeout_seconds=timeout_seconds,
                    allow_domains=allow_domains,
                )
            )
        deduped: List[Dict[str, Any]] = []
        for link in links:
            if any(item["url"] == link["url"] for item in deduped):
                continue
            deduped.append(link)
            if len(deduped) >= max_sources_per_symbol:
                break
        sources: List[Dict[str, Any]] = []
        for link in deduped:
            fetched = _fetch_page(link["url"], timeout_seconds=timeout_seconds, max_chars=max_page_chars)
            source = {
                "source_id": _source_id(link["url"]),
                "symbol": candidate["symbol"],
                "title": link.get("title", ""),
                "url": link["url"],
                "query": link.get("query", ""),
                "fetch_ok": bool(fetched.get("ok")),
                "status_code": int(fetched.get("status_code", 0) or 0),
                "content_hash": _text(fetched.get("content_hash")),
                "text": _text(fetched.get("text")),
                "error": _text(fetched.get("error")),
            }
            source_rows.append(source)
            if source["fetch_ok"] and source["text"]:
                sources.append(source)
        review = _llm_review_candidate(config, cfg, candidate, sources)
        review["candidate"] = candidate
        review["source_count"] = len(sources)
        audit_rows.append(review)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ensure_dir(out_root / stamp)
    grade_counts: Dict[str, int] = {}
    verdict_counts: Dict[str, int] = {}
    for row in audit_rows:
        grade = _text(row.get("evidence_grade")).upper() or "NA"
        verdict = _text(row.get("verdict")) or "unknown"
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    summary = {
        "enabled": True,
        "ok": True,
        "ran": True,
        "candidate_pool_path": str(pool_path),
        "candidate_count": len(candidates),
        "audited_count": len(audit_rows),
        "source_count": len(source_rows),
        "grade_counts": grade_counts,
        "verdict_counts": verdict_counts,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "artifact_dir": str(run_dir),
    }
    (run_dir / "evidence_audit_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "evidence_audit_reviews.json").write_text(json.dumps(audit_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "evidence_audit_sources.json").write_text(json.dumps(source_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = ensure_dir(out_root / "latest")
    (latest / "evidence_audit_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (latest / "evidence_audit_reviews.json").write_text(json.dumps(audit_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (latest / "evidence_audit_sources.json").write_text(json.dumps(source_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def load_latest_evidence_reviews(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    cfg = dict(config.get("evidence_audit", {}) or {})
    if not bool(cfg.get("enabled", True)):
        return {}
    paths = dict(config.get("paths", {}) or {})
    root = Path(_text(cfg.get("artifact_root")) or Path(paths.get("portfolio_output_root", "data/portfolio_recommendation_v6")) / "evidence_audit_v1").resolve()
    path = root / "latest" / "evidence_audit_reviews.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in list(rows or []):
        symbol = _symbol(dict(row or {}).get("symbol") or dict(dict(row or {}).get("candidate", {}) or {}).get("symbol"))
        if symbol:
            out[symbol] = dict(row or {})
    return out


def apply_evidence_gate(df: pd.DataFrame, config: Dict[str, Any]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    cfg = dict(config.get("evidence_audit", {}) or {})
    if df.empty or not bool(cfg.get("portfolio_gate_enabled", True)):
        return df.copy(), {"enabled": bool(cfg.get("portfolio_gate_enabled", True)), "applied": False}
    reviews = load_latest_evidence_reviews(config)
    out = df.copy()
    if not reviews:
        return out, {"enabled": True, "applied": False, "reason": "no_latest_evidence_reviews"}
    symbol_col = "ts_code" if "ts_code" in out.columns else ("code" if "code" in out.columns else "")
    if not symbol_col:
        return out, {"enabled": True, "applied": False, "reason": "missing_symbol_column"}
    multipliers = dict(cfg.get("grade_weight_multipliers", {}) or {})
    default_multipliers = {"A": 1.10, "B": 1.04, "C": 0.92, "D": 0.55, "F": 0.0}
    default_multipliers.update({str(k).upper(): _float(v, default_multipliers.get(str(k).upper(), 1.0)) for k, v in multipliers.items()})
    grades: List[str] = []
    verdicts: List[str] = []
    applied = 0
    for idx, row in out.iterrows():
        symbol = _symbol(row.get(symbol_col))
        review = reviews.get(symbol, {})
        grade = _text(review.get("evidence_grade")).upper() if review else ""
        verdict = _text(review.get("verdict")) if review else ""
        grades.append(grade or "NA")
        verdicts.append(verdict)
        if grade and "portfolio_weight" in out.columns:
            out.at[idx, "portfolio_weight"] = _float(out.at[idx, "portfolio_weight"]) * default_multipliers.get(grade, 1.0)
            applied += 1
    out["evidence_grade"] = grades
    out["evidence_verdict"] = verdicts
    return out, {
        "enabled": True,
        "applied": applied > 0,
        "matched_symbols": applied,
        "grade_counts": {str(k): int(v) for k, v in pd.Series(grades).value_counts().to_dict().items()},
        "grade_weight_multipliers": default_multipliers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run small-pool non-structured evidence audit.")
    parser.add_argument("--config", default="", help="Optional runtime config JSON path.")
    parser.add_argument("--candidate-pool", default="", help="Candidate pool CSV path.")
    parser.add_argument("--limit", type=int, default=0, help="Max candidates to audit.")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else build_runtime_config()
    result = run_evidence_audit(
        config,
        candidate_pool_path=Path(args.candidate_pool).resolve() if args.candidate_pool else None,
        limit=args.limit or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if bool(result.get("ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
