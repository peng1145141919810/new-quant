from __future__ import annotations

import json
import re
import ssl
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from ...config_utils import ensure_dir
from .common import freshness_weight, normalize_date, safe_float, safe_text

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'

OFFICIAL_DISCOVERY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'miit_electronics_ops': {
        'discovery_url': 'https://www.miit.gov.cn/gxsj/tjfx/dzxx/',
        'link_include_keywords': ['电子信息', '运行情况'],
        'link_exclude_keywords': ['首页', '工业和信息化部'],
    },
    'miit_electronics_ops_h1_2025': {
        'discovery_url': 'https://www.miit.gov.cn/jgsj/yxj/xxfb/',
        'link_include_keywords': ['电子信息', '运行情况'],
        'link_exclude_keywords': ['首页', '工业和信息化部'],
    },
    'miit_lithium_battery_ops': {
        'discovery_url': 'https://www.miit.gov.cn/gyhxxhb/jgsj/dzxxs/dzjc/',
        'link_include_keywords': ['锂离子电池', '运行情况'],
        'link_exclude_keywords': ['首页', '工业和信息化部'],
    },
    'miit_petrochemical_growth_plan': {
        'discovery_url': 'https://wap.miit.gov.cn/jgsj/ycls/shhg/',
        'link_include_keywords': ['石化', '化工'],
        'link_exclude_keywords': ['首页', '工业和信息化部'],
    },
    'miit_aluminum_plan': {
        'discovery_url': 'https://www.miit.gov.cn/jgsj/ycls/ysjs/',
        'link_include_keywords': ['铝', '有色'],
        'link_exclude_keywords': ['首页', '工业和信息化部'],
    },
    'caict_phone_market': {
        'discovery_url': 'https://gma.caict.ac.cn/plat/news',
        'link_include_keywords': ['手机市场', '运行分析'],
        'link_exclude_keywords': ['业内新闻', '平台资讯'],
    },
    'nbs_industrial_output': {
        'discovery_url': 'https://www.stats.gov.cn/sj/zxfb/',
        'link_include_keywords': ['工业增加值'],
    },
    'nbs_capacity_utilization': {
        'discovery_url': 'https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/',
        'link_include_keywords': ['产能利用率'],
    },
    'nbs_ppi': {
        'discovery_url': 'https://www.stats.gov.cn/sj/zxfb/',
        'link_include_keywords': ['工业生产者价格'],
    },
    'nbs_material_prices': {
        'discovery_url': 'https://www.stats.gov.cn/sj/zxfb/',
        'link_include_keywords': ['流通领域', '生产资料', '市场价格'],
    },
    'nbs_pmi': {
        'discovery_url': 'https://www.stats.gov.cn/sj/zxfb/',
        'link_include_keywords': ['采购经理指数', 'PMI'],
    },
    'gov_trade_goods_trade': {
        'discovery_url': 'https://www.gov.cn/lianbo/bumen/',
        'link_include_keywords': ['货物贸易', '海关总署'],
        'link_exclude_keywords': ['服务外包', '会见', '国务院常务会议'],
    },
    'gov_nea_power_stats': {
        'discovery_url': 'https://www.gov.cn/lianbo/bumen/',
        'link_include_keywords': ['电力工业', '国家能源局'],
        'link_exclude_keywords': ['服务外包', '会见', '国务院常务会议'],
    },
    'pbc_social_financing': {
        'discovery_url': 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html',
        'link_include_keywords': ['社会融资规模'],
        'link_exclude_keywords': ['RSS', '会见'],
    },
    'pbc_financial_stats': {
        'discovery_url': 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html',
        'link_include_keywords': ['金融统计数据'],
        'link_exclude_keywords': ['RSS', '会见'],
    },
}


def strip_html(html_text: str) -> str:
    text = re.sub(r'<script[\s\S]*?</script>', ' ', html_text, flags=re.IGNORECASE)
    text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_title(html_text: str) -> str:
    for pattern in [r'<title[^>]*>(.*?)</title>', r'<h1[^>]*>(.*?)</h1>']:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return strip_html(match.group(1))[:180]
    return ''


def extract_date(html_text: str) -> str:
    clean = strip_html(html_text)
    prioritized_patterns = [
        r'(?:发布时间|文章来源|来源|时间)\s*[:：]?\s*(20\d{2}-\d{1,2}-\d{1,2})',
        r'(?:发布时间|文章来源|来源|时间)\s*[:：]?\s*(20\d{2}/\d{1,2}/\d{1,2})',
        r'(?:发布时间|文章来源|来源|时间)\s*[:：]?\s*(20\d{2}年\d{1,2}月\d{1,2}日)',
    ]
    for pattern in prioritized_patterns:
        match = re.search(pattern, clean)
        if not match:
            continue
        text = match.group(1)
        if '年' in text:
            return text.replace('年', '-').replace('月', '-').replace('日', '')
        return text.replace('/', '-')
    patterns = [
        r'(20\d{2}-\d{2}-\d{2})',
        r'(20\d{2}/\d{2}/\d{2})',
        r'(20\d{2}年\d{1,2}月\d{1,2}日)',
    ]
    for pattern in patterns:
        match = re.search(pattern, clean)
        if not match:
            continue
        text = match.group(1)
        if '年' in text:
            return text.replace('年', '-').replace('月', '-').replace('日', '')
        return text.replace('/', '-')
    return ''


def fetch_html(url: str, timeout: int) -> str:
    req = Request(url, headers={'User-Agent': USER_AGENT})
    try:
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or ''
    except Exception as exc:
        if 'CERTIFICATE_VERIFY_FAILED' not in str(exc).upper():
            raise
        ctx = ssl._create_unverified_context()
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or ''
    tried = []
    for name in [charset, 'utf-8', 'utf-8-sig', 'gb18030', 'gbk']:
        encoding = safe_text(name)
        if not encoding or encoding.lower() in tried:
            continue
        tried.append(encoding.lower())
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode('utf-8', errors='ignore')


def keyword_signal(text: str, positive_keywords: List[str], negative_keywords: List[str]) -> Dict[str, Any]:
    content = safe_text(text)
    pos_hits = [kw for kw in positive_keywords if kw and kw.lower() in content.lower()]
    neg_hits = [kw for kw in negative_keywords if kw and kw.lower() in content.lower()]
    raw_score = 0.14 * len(pos_hits) - 0.14 * len(neg_hits)
    return {
        'score': max(-0.5, min(0.5, round(raw_score, 4))),
        'positive_hits': pos_hits,
        'negative_hits': neg_hits,
    }


def cache_path(snapshot_root: Path, source_id: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', source_id)
    return snapshot_root / f'{safe}.json'


def load_cache(path: Path, cache_hours: int) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    fetched_at = safe_text(payload.get('fetched_at'))
    if not fetched_at:
        return None
    try:
        dt = datetime.fromisoformat(fetched_at)
    except Exception:
        return None
    if datetime.now() - dt > timedelta(hours=cache_hours):
        return None
    return payload


def domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ''


def _entry_with_discovery_defaults(entry: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(entry or {})
    source_id = safe_text(merged.get('source_id'))
    defaults = dict(OFFICIAL_DISCOVERY_DEFAULTS.get(source_id, {}) or {})
    for key, value in defaults.items():
        if not merged.get(key):
            merged[key] = value
    return merged


def extract_links(html_text: str, base_url: str) -> List[Dict[str, str]]:
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)
    items: List[Dict[str, str]] = []
    seen: set[str] = set()
    for href, raw_text in pattern.findall(html_text):
        url = urljoin(base_url, safe_text(href))
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            continue
        clean_url = url.split('#', 1)[0]
        if clean_url in seen:
            continue
        seen.add(clean_url)
        items.append({'url': clean_url, 'text': strip_html(raw_text)[:180]})
    return items


def _candidate_keyword_score(source: Dict[str, Any], title: str, link_text: str, url: str) -> float:
    blob = ' '.join([safe_text(source.get('source_name')), title, link_text, url]).lower()
    include_keywords = [safe_text(item) for item in list(source.get('link_include_keywords', []) or []) if safe_text(item)]
    exclude_keywords = [safe_text(item) for item in list(source.get('link_exclude_keywords', []) or []) if safe_text(item)]
    score = float(sum(1.0 for item in include_keywords if item.lower() in blob))
    score -= float(sum(1.25 for item in exclude_keywords if item.lower() in blob))
    if url.lower().endswith('/index.html') or url.lower().rstrip('/').count('/') <= 2:
        score -= 1.0
    if any(token in url.lower() for token in ['/index.html', '/index.htm', '/rss', 'plat/news']):
        score -= 0.75
    return score


def _is_generic_page(title: str, url: str) -> bool:
    text = ' '.join([safe_text(title), safe_text(url)]).lower()
    generic_tokens = [
        'rss订阅',
        'rss generator',
        '中华人民共和国工业和信息化部',
        '国家统计局',
        '上海期货交易所',
        '业内新闻',
        'index.html',
    ]
    return any(token.lower() in text for token in generic_tokens)


def _llm_pick_candidate(
    config: Dict[str, Any],
    *,
    source: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    source_fetch_cfg = dict(config.get('industry_router', {}).get('source_fetch', {}) or {})
    if not bool(source_fetch_cfg.get('llm_discovery_enabled', False)):
        return {'ok': False, 'error': 'llm_discovery_disabled'}
    provider = safe_text(source_fetch_cfg.get('llm_provider') or 'deepseek_worker').lower()
    timeout_seconds = int(source_fetch_cfg.get('llm_timeout_seconds', 20) or 20)
    max_candidates = max(int(source_fetch_cfg.get('llm_max_candidates', 5) or 5), 2)
    shortlist = list(candidates[:max_candidates])
    if not shortlist:
        return {'ok': False, 'error': 'no_candidates'}
    try:
        from ...llm_router import LLMRouter
    except Exception as exc:
        return {'ok': False, 'error': f'llm_router_import_failed:{exc}'}
    router = LLMRouter(
        provider_cfg=dict(config.get('providers', {}) or {}),
        schema_root=None,
        local_ollama_cfg=dict(config.get('local_ollama', {}) or {}),
    )
    source_name = safe_text(source.get('source_name'))
    include_keywords = [safe_text(item) for item in list(source.get('link_include_keywords', []) or []) if safe_text(item)]
    exclude_keywords = [safe_text(item) for item in list(source.get('link_exclude_keywords', []) or []) if safe_text(item)]
    system_prompt = (
        '你是A股行业研究数据抓取的页面筛选器。'
        '任务是在多个候选网页里选出最像目标官方统计/运行情况正文的那一页。'
        '必须输出严格 JSON，不要输出解释文本。'
        '如果候选都不合格，必须返回 reject。'
    )
    user_prompt = json.dumps(
        {
            'task': 'pick_best_official_page',
            'source_name': source_name,
            'source_id': safe_text(source.get('source_id')),
            'include_keywords': include_keywords,
            'exclude_keywords': exclude_keywords,
            'expected_traits': [
                'title matches target topic',
                'page is an article/detail page instead of homepage/listing/RSS page',
                'publish_date is explicit or inferable',
                'body snippet contains topic-specific text instead of generic navigation',
            ],
            'output_schema': {
                'decision': 'pick|reject',
                'picked_index': 'int',
                'confidence': 'float_0_to_1',
                'reason': 'string',
            },
            'candidates': [
                {
                    'index': idx,
                    'url': safe_text(item.get('resolved_url')),
                    'title': safe_text(item.get('title')),
                    'publish_date': safe_text(item.get('publish_date')),
                    'keyword_score': float(item.get('keyword_score', 0.0) or 0.0),
                    'resolution_score': float(item.get('resolution_score', 0.0) or 0.0),
                    'is_generic_page': bool(item.get('is_generic_page', False)),
                    'preview': safe_text(item.get('preview'))[:500],
                }
                for idx, item in enumerate(shortlist)
            ],
        },
        ensure_ascii=False,
    )
    if provider == 'local_ollama':
        result = router.call_local_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds)
    elif provider == 'openai_research':
        result = router.call_research_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds)
    else:
        result = router.call_worker_json_detailed(system_prompt=system_prompt, user_prompt=user_prompt, timeout_seconds=timeout_seconds)
    data = dict(result.get('data', {}) or {})
    decision = safe_text(data.get('decision')).lower()
    try:
        picked_index = int(data.get('picked_index'))
    except Exception:
        picked_index = -1
    try:
        confidence = float(data.get('confidence', 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    return {
        'ok': bool(result.get('ok', False)),
        'provider': safe_text(result.get('provider')),
        'model': safe_text(result.get('model')),
        'decision': decision,
        'picked_index': picked_index,
        'confidence': confidence,
        'reason': safe_text(data.get('reason') or result.get('error_message')),
    }


def resolve_official_page(source: Dict[str, Any], *, timeout: int, as_of_date: str = '', config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    entry = _entry_with_discovery_defaults(source)
    direct_url = safe_text(entry.get('url'))
    discovery_url = safe_text(entry.get('discovery_url'))
    source_id = safe_text(entry.get('source_id'))
    if not discovery_url or discovery_url == direct_url:
        html = fetch_html(direct_url, timeout=timeout)
        title = extract_title(html) or safe_text(entry.get('source_name'))
        publish_date = normalize_date(extract_date(html))
        return {
            'source_id': source_id,
            'status': 'ok',
            'resolved_url': direct_url,
            'html': html,
            'title': title,
            'publish_date': publish_date,
            'discovery_url': discovery_url,
            'candidate_count': 1,
        }

    discovery_html = fetch_html(discovery_url, timeout=timeout)
    links = extract_links(discovery_html, discovery_url)
    direct_domain = domain_from_url(direct_url)
    candidates: List[Dict[str, str]] = [{'url': direct_url, 'text': safe_text(entry.get('source_name'))}]
    for item in links:
        url = safe_text(item.get('url'))
        if direct_domain and domain_from_url(url) != direct_domain:
            continue
        if not url:
            continue
        candidates.append({'url': url, 'text': safe_text(item.get('text'))})
    ranked = sorted(
        candidates,
        key=lambda item: _candidate_keyword_score(entry, '', safe_text(item.get('text')), safe_text(item.get('url'))),
        reverse=True,
    )
    max_candidates = max(int(entry.get('max_discovery_candidates') or 12), 1)
    best_payload: Dict[str, Any] | None = None
    evaluated: List[Dict[str, Any]] = []
    errors: List[str] = []
    for item in ranked[:max_candidates]:
        url = safe_text(item.get('url'))
        if not url:
            continue
        try:
            html = fetch_html(url, timeout=timeout)
            title = extract_title(html) or safe_text(item.get('text')) or safe_text(entry.get('source_name'))
            publish_date = normalize_date(extract_date(html))
            freshness = freshness_weight(publish_date=publish_date, as_of_date=as_of_date)
            keyword_score = _candidate_keyword_score(entry, title, safe_text(item.get('text')), url)
            score = keyword_score + 2.0 * freshness
            if _is_generic_page(title, url):
                score -= 3.0
            if list(entry.get('link_include_keywords', []) or []) and keyword_score <= 0:
                score -= 2.0
            payload = {
                'source_id': source_id,
                'status': 'ok',
                'resolved_url': url,
                'html': html,
                'title': title,
                'publish_date': publish_date,
                'discovery_url': discovery_url,
                'candidate_count': min(len(ranked), max_candidates),
                'freshness_weight': round(freshness, 4),
                'keyword_score': round(keyword_score, 4),
                'resolution_score': round(score, 4),
                'preview': strip_html(html)[:500],
                'is_generic_page': _is_generic_page(title, url),
            }
            evaluated.append(payload)
            if best_payload is None or float(payload.get('resolution_score') or 0.0) > float(best_payload.get('resolution_score') or 0.0):
                best_payload = payload
        except Exception as exc:
            errors.append(f'{url}: {exc}')
    acceptable = (
        best_payload is not None
        and float(best_payload.get('resolution_score') or 0.0) > 0.5
        and not _is_generic_page(safe_text(best_payload.get('title')), safe_text(best_payload.get('resolved_url')))
    )
    ambiguous = False
    if len(evaluated) >= 2:
        ordered = sorted(evaluated, key=lambda item: float(item.get('resolution_score') or 0.0), reverse=True)
        ambiguous = abs(float(ordered[0].get('resolution_score') or 0.0) - float(ordered[1].get('resolution_score') or 0.0)) < 1.0
    if config is not None and evaluated and (not acceptable or ambiguous):
        llm_pick = _llm_pick_candidate(config, source=entry, candidates=sorted(evaluated, key=lambda item: float(item.get('resolution_score') or 0.0), reverse=True))
        if llm_pick.get('ok') and llm_pick.get('decision') == 'pick':
            picked_index = int(llm_pick.get('picked_index', -1) or -1)
            ordered = sorted(evaluated, key=lambda item: float(item.get('resolution_score') or 0.0), reverse=True)
            if 0 <= picked_index < len(ordered):
                chosen = dict(ordered[picked_index])
                if not bool(chosen.get('is_generic_page', False)):
                    chosen['llm_selected'] = True
                    chosen['llm_provider'] = safe_text(llm_pick.get('provider'))
                    chosen['llm_model'] = safe_text(llm_pick.get('model'))
                    chosen['llm_confidence'] = float(llm_pick.get('confidence', 0.0) or 0.0)
                    chosen['llm_reason'] = safe_text(llm_pick.get('reason'))
                    return chosen
    if acceptable:
        return best_payload
    raise RuntimeError('; '.join(errors[:3]) or f'failed to resolve official page for {source_id or direct_url}')


def fetch_source_snapshots(
    config: Dict[str, Any],
    source_contracts: Dict[str, Any],
    output_root: Path,
    as_of_date: str,
) -> Dict[str, Any]:
    source_cfg = dict(config.get('industry_router', {}).get('source_fetch', {}) or {})
    if not bool(source_cfg.get('enabled', False)):
        return {'enabled': False, 'items': [], 'state_rows': [], 'summary': {'status': 'disabled'}}

    timeout = int(source_cfg.get('timeout_seconds', 12) or 12)
    cache_hours = int(source_cfg.get('cache_hours', 12) or 12)
    max_sources = int(source_cfg.get('max_sources_per_run', 12) or 12)
    snapshot_root = ensure_dir(output_root / 'source_snapshots')
    items: List[Dict[str, Any]] = []
    state_rows: List[Dict[str, Any]] = []
    fetched_count = 0

    for mechanism, bucket in dict(source_contracts.get('mechanism_groups', {}) or {}).items():
        for category in ['industry_state_sources', 'macro_context_sources']:
            for entry in list(dict(bucket).get(category, []) or []):
                if not isinstance(entry, dict):
                    continue
                entry = _entry_with_discovery_defaults(entry)
                if safe_text(entry.get('mode')) != 'official_page':
                    continue
                if fetched_count >= max_sources:
                    break
                source_id = safe_text(entry.get('source_id')) or safe_text(entry.get('source_name'))
                local_cache_path = cache_path(snapshot_root=snapshot_root, source_id=source_id)
                cached = load_cache(path=local_cache_path, cache_hours=cache_hours)
                if cached is not None:
                    payload = cached
                else:
                    url = safe_text(entry.get('url'))
                    payload = {
                        'source_id': source_id,
                        'source_name': safe_text(entry.get('source_name')),
                        'mechanism_group': mechanism,
                        'category': category,
                        'url': url,
                        'domain': domain_from_url(url),
                        'status': 'error',
                        'fetched_at': datetime.now().isoformat(timespec='seconds'),
                        'publish_date': '',
                        'title': '',
                        'summary': '',
                        'signal_score': 0.0,
                        'confidence': 0.0,
                        'positive_hits': [],
                        'negative_hits': [],
                        'source_weight': 0.0,
                        'category_weight': 0.0,
                        'freshness_weight': 0.0,
                        'error': '',
                    }
                    try:
                        resolved = resolve_official_page(entry, timeout=timeout, as_of_date=as_of_date, config=config)
                        html_text = safe_text(resolved.get('html'))
                        url = safe_text(resolved.get('resolved_url')) or url
                        text = strip_html(html_text)
                        title = safe_text(resolved.get('title')) or extract_title(html_text) or safe_text(entry.get('source_name'))
                        publish_date = safe_text(resolved.get('publish_date')) or extract_date(html_text)
                        summary = text[:360]
                        kw = keyword_signal(
                            text=' '.join([title, summary]),
                            positive_keywords=list(entry.get('positive_keywords', []) or []),
                            negative_keywords=list(entry.get('negative_keywords', []) or []),
                        )
                        fresh = freshness_weight(publish_date=publish_date, as_of_date=as_of_date)
                        source_weight = safe_float(entry.get('source_weight'), 1.0)
                        category_weight = safe_float(entry.get('category_weight'), 1.0 if category == 'industry_state_sources' else 0.85)
                        confidence = round(0.75 * fresh, 4)
                        payload.update(
                            {
                                'status': 'ok',
                                'publish_date': normalize_date(publish_date),
                                'title': title,
                                'summary': summary,
                                'signal_score': round(float(kw['score']) * fresh * source_weight * category_weight, 4),
                                'confidence': confidence,
                                'positive_hits': list(kw['positive_hits']),
                                'negative_hits': list(kw['negative_hits']),
                                'source_weight': round(source_weight, 4),
                                'category_weight': round(category_weight, 4),
                                'freshness_weight': round(fresh, 4),
                                'resolved_url': url,
                                'discovery_url': safe_text(resolved.get('discovery_url')),
                                'candidate_count': int(resolved.get('candidate_count') or 1),
                                'error': '',
                            }
                        )
                    except Exception as exc:
                        payload['error'] = str(exc)[:300]
                    local_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                items.append(payload)
                fetched_count += 1
                if safe_text(payload.get('status')) == 'ok':
                    state_rows.append(
                        {
                            'date': as_of_date,
                            'mechanism_group': mechanism,
                            'source_id': source_id,
                            'source_name': safe_text(payload.get('source_name')),
                            'category': category,
                            'source_signal_score': safe_float(payload.get('signal_score'), 0.0),
                            'confidence': safe_float(payload.get('confidence'), 0.0),
                            'publish_date': safe_text(payload.get('publish_date')),
                            'title': safe_text(payload.get('title')),
                            'summary': safe_text(payload.get('summary')),
                            'url': safe_text(payload.get('url')),
                            'source_weight': safe_float(payload.get('source_weight'), safe_float(entry.get('source_weight'), 1.0)),
                            'category_weight': safe_float(payload.get('category_weight'), safe_float(entry.get('category_weight'), 1.0)),
                            'freshness_weight': safe_float(payload.get('freshness_weight'), 0.45),
                            'positive_hits': '|'.join(list(payload.get('positive_hits', []) or [])),
                            'negative_hits': '|'.join(list(payload.get('negative_hits', []) or [])),
                        }
                    )
            if fetched_count >= max_sources:
                break
        if fetched_count >= max_sources:
            break

    index_path = snapshot_root / 'source_snapshot_index.json'
    items_path = snapshot_root / 'source_snapshot_items.json'
    index_path.write_text(
        json.dumps(
            {
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'as_of_date': as_of_date,
                'enabled': True,
                'count': len(items),
                'max_sources_per_run': max_sources,
                'ok_count': sum(1 for item in items if safe_text(item.get('status')) == 'ok'),
                'error_count': sum(1 for item in items if safe_text(item.get('status')) != 'ok'),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')

    by_mechanism: Dict[str, Dict[str, Any]] = {}
    for mechanism in sorted(set(item['mechanism_group'] for item in state_rows)):
        subset = [item for item in state_rows if item['mechanism_group'] == mechanism]
        by_mechanism[mechanism] = {
            'source_count': len(subset),
            'avg_signal_score': round(sum(safe_float(item.get('source_signal_score')) for item in subset) / max(len(subset), 1), 4),
            'top_sources': [item['source_name'] for item in sorted(subset, key=lambda row: abs(safe_float(row.get('source_signal_score'))), reverse=True)[:3]],
        }

    return {
        'enabled': True,
        'items': items,
        'state_rows': state_rows,
        'summary': {
            'status': 'ok',
            'index_path': str(index_path),
            'items_path': str(items_path),
            'source_state_path': str(output_root / 'source_state_daily.csv'),
            'max_sources_per_run': max_sources,
            'ok_count': sum(1 for item in items if safe_text(item.get('status')) == 'ok'),
            'error_count': sum(1 for item in items if safe_text(item.get('status')) != 'ok'),
            'by_mechanism': by_mechanism,
        },
    }
