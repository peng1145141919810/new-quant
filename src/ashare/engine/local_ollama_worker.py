# -*- coding: utf-8 -*-
"""本地 Ollama 事件预处理器。"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import requests

from .json_parse_utils import parse_json_object_loose


_OLLAMA_EVENT_HEALTH_CACHE: Dict[str, Dict[str, Any]] = {}
_OLLAMA_EVENT_COOLDOWN_CACHE: Dict[str, float] = {}


VALID_EVENT_TYPES = {
    "财务业绩", "分红回购", "增减持", "并购重组",
    "重大合同", "监管处罚", "停复牌", "诉讼仲裁", "其他",
}


RULE_MAP = [
    ("业绩预告", "财务业绩"),
    ("业绩快报", "财务业绩"),
    ("年度报告", "财务业绩"),
    ("半年度报告", "财务业绩"),
    ("一季度报告", "财务业绩"),
    ("三季度报告", "财务业绩"),
    ("回购", "分红回购"),
    ("分红", "分红回购"),
    ("增持", "增减持"),
    ("减持", "增减持"),
    ("收购", "并购重组"),
    ("重组", "并购重组"),
    ("中标", "重大合同"),
    ("合同", "重大合同"),
    ("处罚", "监管处罚"),
    ("问询", "监管处罚"),
    ("停牌", "停复牌"),
    ("复牌", "停复牌"),
    ("诉讼", "诉讼仲裁"),
    ("仲裁", "诉讼仲裁"),
]


def _provider_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """读取本地模型配置。

    Args:
        config: 运行配置。

    Returns:
        Dict[str, Any]: 本地模型配置。
    """
    return dict(config.get("local_ollama", {}) or {})


def _cache_key(base_url: str, model: str) -> str:
    return f"{str(base_url or '').rstrip('/')}::{str(model or '').strip()}"


def _in_cooldown(cache_key: str) -> bool:
    return float(_OLLAMA_EVENT_COOLDOWN_CACHE.get(cache_key, 0.0) or 0.0) > time.time()


def _mark_cooldown(cache_key: str, cooldown_seconds: int) -> None:
    _OLLAMA_EVENT_COOLDOWN_CACHE[cache_key] = time.time() + max(int(cooldown_seconds or 0), 1)


def _ollama_ready(base_url: str, timeout_seconds: float = 1.5, cache_ttl_seconds: int = 20) -> bool:
    root = str(base_url or "").rstrip("/")
    now_ts = time.time()
    cached = dict(_OLLAMA_EVENT_HEALTH_CACHE.get(root, {}) or {})
    if cached and float(cached.get("expires_at", 0.0) or 0.0) >= now_ts:
        return bool(cached.get("ok", False))
    ok = False
    try:
        resp = requests.get(f"{root}/api/tags", timeout=max(float(timeout_seconds or 1.0), 0.5))
        ok = bool(resp.ok)
    except Exception:
        ok = False
    _OLLAMA_EVENT_HEALTH_CACHE[root] = {"ok": ok, "expires_at": now_ts + max(int(cache_ttl_seconds or 0), 1)}
    return ok


def _normalize_direction(raw: Any) -> str:
    """把模型返回的中文方向词归一到内部枚举。

    Args:
        raw: 模型原始方向字段。

    Returns:
        str: positive/negative/uncertain，无法判断时返回空串交给规则兜底。
    """
    t = str(raw or "").strip().lower()
    if not t:
        return ""
    if any(k in t for k in ("利好", "正面", "积极", "利多", "positive", "bull")):
        return "positive"
    if any(k in t for k in ("利空", "负面", "消极", "利淡", "negative", "bear")):
        return "negative"
    if any(k in t for k in ("中性", "不确定", "neutral", "uncertain")):
        return "uncertain"
    return ""


def build_prompt(title: str, body: str = "") -> str:
    """构造事件抽取提示词（标题 + 可选正文摘录）。

    Args:
        title: 标题文本。
        body: 公告/新闻正文（可为空）。下载到的 PDF 正文会从这里喂进来。

    Returns:
        str: 提示词。
    """
    body_text = str(body or "").strip()
    if body_text:
        material = f"标题：{title}\n\n正文摘录（可能截断，仅用于判断方向与重要性）：\n{body_text[:1500]}"
    else:
        material = f"标题：{title}"
    return f"""
你是量化研究系统里的事件预处理器。
请只输出 JSON，不要输出解释，不要输出 markdown，不要输出代码块。

对下面这条公告/新闻做结构化提取：

{material}

判断方向时要理解语义，注意"终止/取消/暂停/放弃"等否定词会反转含义：
例如"终止减持计划"对股价是利好，"终止股份回购"是利空。

输出格式必须严格为：
{{
  "event_type": "从以下类别中选一个：财务业绩/分红回购/增减持/并购重组/重大合同/监管处罚/停复牌/诉讼仲裁/其他",
  "entity": "主体名称，未知就填空字符串",
  "direction": "从以下选一个：利好/利空/中性",
  "importance": 0到10之间的整数,
  "summary": "一句中文摘要"
}}
""".strip()


def rule_fallback_for_title(title: str) -> Dict[str, Any]:
    """规则兜底。

    Args:
        title: 标题文本。

    Returns:
        Dict[str, Any]: 兜底结构化结果。
    """
    event_type = "其他"
    for keyword, mapped in RULE_MAP:
        if keyword in title:
            event_type = mapped
            break
    importance = 3
    if event_type in {"财务业绩", "并购重组", "重大合同", "监管处罚", "停复牌", "诉讼仲裁"}:
        importance = 7
    elif event_type in {"分红回购", "增减持"}:
        importance = 5
    return {
        "event_type": event_type,
        "entity": "",
        "direction": "",
        "importance": importance,
        "summary": title[:60],
        "extract_backend": "rule_fallback",
        "extract_ok": False,
    }


def parse_single_title(title: str, config: Dict[str, Any], body: str = "") -> Dict[str, Any]:
    """调用本地 Ollama 处理单条公告/新闻。

    Args:
        title: 标题文本。
        config: 运行配置。
        body: 正文（可选，下载到的 PDF 正文喂这里）。

    Returns:
        Dict[str, Any]: 结构化结果。
    """
    provider = _provider_cfg(config)
    base_url = str(provider.get("base_url", "http://localhost:11434")).rstrip("/")
    model = str(provider.get("event_extract_model") or provider.get("model", "qwen2.5:7b") or "qwen2.5:7b")
    timeout_seconds = int(
        provider.get("event_extract_timeout_seconds", provider.get("timeout_seconds", 120)) or 120
    )
    cooldown_seconds = max(int(provider.get("event_extract_cooldown_seconds", 45) or 45), 5)
    cache_key = _cache_key(base_url, model)
    if _in_cooldown(cache_key):
        raise RuntimeError("local_ollama_recent_timeout_cooldown")
    if not _ollama_ready(base_url, timeout_seconds=min(timeout_seconds / 6.0, 1.5)):
        _mark_cooldown(cache_key, max(cooldown_seconds // 2, 10))
        raise RuntimeError("local_ollama_unreachable")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(title, body)}],
        "stream": False,
        "options": {"temperature": 0},
    }

    try:
        resp = requests.post(f"{base_url}/api/chat", json=payload, timeout=timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
        content = str(data.get("message", {}).get("content", "") or "").strip()
        parsed = parse_json_object_loose(content)
        if not parsed:
            raise ValueError("local_ollama_non_json_response")
    except (requests.Timeout, requests.ConnectionError):
        _mark_cooldown(cache_key, cooldown_seconds)
        _OLLAMA_EVENT_HEALTH_CACHE[base_url] = {"ok": False, "expires_at": time.time() + max(cooldown_seconds // 2, 10)}
        raise

    event_type = str(parsed.get("event_type", "其他") or "其他")
    if event_type not in VALID_EVENT_TYPES:
        event_type = "其他"

    importance_raw = parsed.get("importance", 0)
    try:
        importance = int(importance_raw)
    except Exception:
        importance = 0
    importance = max(0, min(10, importance))

    return {
        "event_type": event_type,
        "entity": str(parsed.get("entity", "") or ""),
        "direction": _normalize_direction(parsed.get("direction")),
        "importance": importance,
        "summary": str(parsed.get("summary", title) or title)[:80],
        "extract_backend": f"ollama_{model.replace(':', '_')}",
        "extract_ok": True,
    }


def batch_parse_titles(items: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """批量处理事件标题。

    Args:
        items: 原始事件列表。
        config: 运行配置。

    Returns:
        List[Dict[str, Any]]: 补充结构化结果后的事件列表。
    """
    outputs: List[Dict[str, Any]] = []
    for item in items:
        title = str(item.get("title") or item.get("raw_title") or "").strip()
        body = str(item.get("content") or item.get("raw_text") or "").strip()
        if not title:
            enriched = dict(item)
            enriched.update(rule_fallback_for_title(title=""))
            outputs.append(enriched)
            continue
        try:
            parsed = parse_single_title(title=title, config=config, body=body)
            enriched = dict(item)
            enriched.update(parsed)
            outputs.append(enriched)
        except Exception as exc:
            enriched = dict(item)
            enriched.update(rule_fallback_for_title(title=title))
            enriched["extract_error"] = str(exc)
            outputs.append(enriched)
    return outputs
