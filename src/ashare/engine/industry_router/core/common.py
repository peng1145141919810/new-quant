from __future__ import annotations

import json
import numbers
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Sequence

LIQUIDITY_BUCKET_BASE_SCORE = {'A': 0.38, 'B': 0.25, 'C': 0.12}
LIQUIDITY_BUCKET_RISK_PENALTY = {'A': 0.02, 'B': 0.05, 'C': 0.10}
LIQUIDITY_BUCKET_PROFILE_SCORE = {'A': 1.0, 'B': 0.72, 'C': 0.45}
LIQUIDITY_RANK = {'A': 0, 'B': 1, 'C': 2}

LOW_MID_HIGH = {'low': 0.2, 'mid': 0.55, 'high': 0.9}
ELASTICITY_BUCKET = {'low': 0.2, 'mid': 0.5, 'high': 0.75, 'extreme': 1.0}
PASS_THROUGH_BUCKET = {'weak': 0.2, 'mid': 0.55, 'strong': 0.9}
DIRECT_RESOURCE_BUCKET = {'theme_only': 0.1, 'indirect_beneficiary': 0.35, 'midstream_material': 0.65, 'direct_resource': 0.95}
DEFENSIVE_BUCKET = {'defensive': 0.85, 'balanced': 0.55, 'offensive': 0.2}
GLOBAL_EXPOSURE_BUCKET = {'domestic_dominant': 0.35, 'dual_engine': 0.68, 'global_dominant': 0.92}
BENEFIT_MODE_BUCKET = {'theme_only': 0.12, 'valuation_link': 0.32, 'capacity_pull': 0.58, 'spec_upgrade': 0.74, 'direct_order': 0.92}
CUSTOMER_ANCHOR_BUCKET = {
    'other': 0.3,
    'terminal_brand': 0.45,
    'equipment_vendor': 0.52,
    'foundry': 0.6,
    'operator': 0.66,
    'domestic_cloud': 0.76,
    'global_cloud': 0.9,
}
STYLE_BUCKET = {'value': 0.62, 'growth': 0.55, 'dividend': 0.88, 'financial': 0.8, 'cyclical': 0.45, 'policy_sensitive': 0.52}
INDUSTRY_ALIAS_GROUPS = {
    '电子': (
        '半导体',
        '芯片',
        '元器件',
        '电子元件',
        '消费电子',
        '光模块',
        '通信设备',
        '通信器件',
        '电信运营',
        '服务器',
        '算力',
        'PCB',
        '晶圆',
    ),
    '化工': (
        '化工原料',
        '化学原料',
        '化学制品',
        '石油化工',
        '煤化工',
        '氯碱',
        '农化',
        '聚氨酯',
        'MDI',
        '甲醇',
        '苯乙烯',
        'PTA',
        'PX',
        '尿素',
        '烧碱',
    ),
    '有色': (
        '有色金属',
        '工业金属',
        '有色资源',
        '铜',
        '铝',
        '氧化铝',
        '电解铜',
        '电解铝',
        '镍',
        '钴',
        '稀土',
        '小金属',
    ),
    '新能源金属': (
        '新能源材料',
        '新能源金属',
        '锂',
        '锂电',
        '锂电池',
        '碳酸锂',
        '氢氧化锂',
        '钴锂资源',
        '工业硅',
        '多晶硅',
        '硅片',
        '光伏材料',
    ),
}
NUMERIC_FACTOR_FIELDS = {
    'price_momentum_score',
    'inventory_tightness_score',
    'trade_flow_verification_score',
    'industry_expansion_score',
    'demand_verification_score',
    'external_demand_score',
    'source_consensus_score',
    'source_state_score',
    'confidence',
}


def safe_text(value: Any) -> str:
    return str(value or '').strip()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def safe_json_text(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        return '{}'


def normalize_symbol(value: Any) -> str:
    text = safe_text(value).upper()
    if not text:
        return ''
    if '.' in text:
        code, suffix = text.split('.', 1)
        return f"{code.zfill(6)}.{suffix.upper()}"
    if text.isdigit():
        suffix = 'SH' if text.startswith(('5', '6', '9')) else 'SZ'
        return f"{text.zfill(6)}.{suffix}"
    return text


def symbol_to_code(symbol: Any) -> str:
    text = normalize_symbol(symbol)
    if not text:
        return ''
    return text.split('.', 1)[0]


def parse_date(value: Any) -> str:
    text = safe_text(value)
    if not text:
        return ''
    return text[:10]


def normalize_date(text: Any) -> str:
    raw = safe_text(text)
    if not raw:
        return ''
    if '年' in raw:
        raw = raw.replace('年', '-').replace('月', '-').replace('日', '')
    raw = raw.replace('/', '-')
    parts = raw.split('-')
    if len(parts) >= 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except Exception:
            return raw[:10]
    return raw[:10]


def parse_confidence(value: Any) -> float:
    text = safe_text(value).lower()
    if text in {'high', 'very_high'}:
        return 0.9
    if text == 'medium':
        return 0.7
    if text == 'low':
        return 0.4
    num = safe_float(value, -1.0)
    if num >= 0:
        return max(0.0, min(1.0, num if num <= 1.0 else num / 100.0))
    return 0.65


def normalize_importance(value: Any) -> float:
    num = safe_float(value, 0.0)
    if num <= 0:
        return 0.0
    if num <= 1.0:
        return num
    if num <= 10.0:
        return min(1.0, num / 10.0)
    return min(1.0, num / 100.0)


def split_exposures(value: Any) -> List[str]:
    text = safe_text(value)
    if not text:
        return []
    parts = [item.strip() for item in text.replace(',', '|').split('|')]
    return [item for item in parts if item]


def signed_direction(direction: Any) -> int:
    text = safe_text(direction).lower()
    if text in {'positive', 'up', 'bullish', '利好'}:
        return 1
    if text in {'negative', 'down', 'bearish', '利空'}:
        return -1
    return 0


def liquidity_base_score(bucket: Any) -> float:
    return LIQUIDITY_BUCKET_BASE_SCORE.get(safe_text(bucket).upper(), 0.18)


def liquidity_risk_penalty(bucket: Any) -> float:
    return LIQUIDITY_BUCKET_RISK_PENALTY.get(safe_text(bucket).upper(), 0.06)


def liquidity_profile_score(bucket: Any) -> float:
    return LIQUIDITY_BUCKET_PROFILE_SCORE.get(safe_text(bucket).upper(), 0.58)


def clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def map_bucket_score(value: Any, mapping: Mapping[str, float], default: float = 0.5) -> float:
    return clip(mapping.get(safe_text(value).lower(), default), 0.0, 1.0)


def classify_regime(score: float, heat: float) -> str:
    state = clip(score, -1.0, 1.0)
    heat_v = clip(heat, 0.0, 1.0)
    if state >= 0.45 and heat_v >= 0.35:
        return 'expansion'
    if state <= -0.45 and heat_v >= 0.35:
        return 'contraction'
    if abs(state) <= 0.12 and heat_v <= 0.18:
        return 'idle'
    if state > 0:
        return 'improving'
    if state < 0:
        return 'weakening'
    return 'neutral'


def freshness_weight(publish_date: str, as_of_date: str) -> float:
    pub = normalize_date(publish_date)
    ref = normalize_date(as_of_date)
    if not pub or not ref:
        return 0.45
    try:
        days = (datetime.strptime(ref, '%Y-%m-%d').date() - datetime.strptime(pub, '%Y-%m-%d').date()).days
    except Exception:
        return 0.45
    if days <= 45:
        return 1.0
    if days <= 120:
        return 0.75
    if days <= 240:
        return 0.45
    return 0.25


def dominant_strings(values: Iterable[str], limit: int = 2) -> List[str]:
    seen: List[str] = []
    for item in values:
        text = safe_text(item)
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def exposure_overlap_score(exposures: Sequence[str], keywords: Sequence[str]) -> float:
    if not exposures or not keywords:
        return 0.0
    tokens = {safe_text(x).lower() for x in exposures if safe_text(x)}
    refs = {safe_text(x).lower() for x in keywords if safe_text(x)}
    if not tokens or not refs:
        return 0.0
    hits = sum(1 for token in tokens if any(token in ref or ref in token for ref in refs))
    return clip(hits / max(len(tokens), 1), 0.0, 1.0)


def sign_consensus(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    positives = sum(1 for item in values if item > 0)
    negatives = sum(1 for item in values if item < 0)
    total = max(len(values), 1)
    return round(abs(positives - negatives) / total, 4)


def mean_or_zero(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(sum(float(x) for x in values) / max(len(values), 1))


def industry_alias_keys(value: Any) -> List[str]:
    text = safe_text(value)
    if not text:
        return []
    keys: List[str] = []

    def _push(item: str) -> None:
        normalized = safe_text(item)
        if normalized and normalized not in keys:
            keys.append(normalized)

    _push(text)
    for canonical, aliases in INDUSTRY_ALIAS_GROUPS.items():
        universe = [canonical, *aliases]
        if any(alias and (alias in text or text in alias) for alias in universe):
            _push(canonical)
    return keys


def merge_factor_payloads(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if not payloads:
        return merged
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key, value in payload.items():
            if key == 'top_products':
                bucket = merged.setdefault(key, [])
                for item in list(value or []):
                    text = safe_text(item)
                    if text and text not in bucket:
                        bucket.append(text)
                continue
            if key in {'source_count', 'evidence_count'}:
                merged[key] = int(merged.get(key, 0) or 0) + safe_int(value, 0)
                continue
            if key == 'matched_industry_keys':
                bucket = merged.setdefault(key, [])
                for item in list(value or []):
                    text = safe_text(item)
                    if text and text not in bucket:
                        bucket.append(text)
                continue
            if key in NUMERIC_FACTOR_FIELDS or isinstance(value, numbers.Number):
                history = merged.setdefault(f'__avg__{key}', [])
                history.append(safe_float(value, 0.0))
                merged[key] = round(sum(history) / max(len(history), 1), 4)
                continue
            if key not in merged or safe_text(merged.get(key)) == '':
                merged[key] = value
    for key in [item for item in list(merged.keys()) if item.startswith('__avg__')]:
        merged.pop(key, None)
    return merged


def resolve_industry_factor_payload(by_industry: Mapping[str, Any] | None, industry_key: Any) -> Dict[str, Any]:
    lookup = dict(by_industry or {})
    alias_keys = industry_alias_keys(industry_key)
    matched_payloads: List[Mapping[str, Any]] = []
    matched_keys: List[str] = []
    for alias in alias_keys:
        payload = lookup.get(alias)
        if isinstance(payload, Mapping) and payload:
            matched_payloads.append(payload)
            matched_keys.append(alias)
    merged = merge_factor_payloads(matched_payloads)
    if matched_keys:
        merged['matched_industry_keys'] = matched_keys
    return merged
