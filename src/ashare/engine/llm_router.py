# -*- coding: utf-8 -*-
"""V6 LLM 路由器。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .json_parse_utils import parse_json_object_loose


_OLLAMA_HEALTH_CACHE: Dict[str, Dict[str, Any]] = {}
_OLLAMA_COOLDOWN_CACHE: Dict[str, float] = {}


def _ollama_cache_key(base_url: str, model: str) -> str:
    return f"{str(base_url or '').rstrip('/')}::{str(model or '').strip()}"


def _ollama_in_cooldown(cache_key: str) -> bool:
    until_ts = float(_OLLAMA_COOLDOWN_CACHE.get(cache_key, 0.0) or 0.0)
    return until_ts > time.time()


def _mark_ollama_cooldown(cache_key: str, cooldown_seconds: int) -> None:
    _OLLAMA_COOLDOWN_CACHE[cache_key] = time.time() + max(int(cooldown_seconds or 0), 1)


def _ollama_ready(base_url: str, timeout_seconds: float, cache_ttl_seconds: int = 20) -> bool:
    root = str(base_url or "").rstrip("/")
    now_ts = time.time()
    cached = dict(_OLLAMA_HEALTH_CACHE.get(root, {}) or {})
    if cached and float(cached.get("expires_at", 0.0) or 0.0) >= now_ts:
        return bool(cached.get("ok", False))
    ok = False
    try:
        resp = requests.get(f"{root}/api/tags", timeout=max(float(timeout_seconds or 1.0), 0.5))
        ok = bool(resp.ok)
    except Exception:
        ok = False
    _OLLAMA_HEALTH_CACHE[root] = {"ok": ok, "expires_at": now_ts + max(int(cache_ttl_seconds or 0), 1)}
    return ok


class DeepSeekChatClient:
    """DeepSeek 低成本执行脑客户端。"""

    def __init__(self, provider_name: str, cfg: Dict[str, Any]):
        """初始化客户端。

        Args:
            provider_name: 提供方名称。
            cfg: 提供方配置。

        Returns:
            None
        """
        self.provider_name = provider_name
        self.cfg = dict(cfg or {})

    def enabled(self) -> bool:
        """判断是否启用。

        Args:
            None

        Returns:
            bool: 是否启用。
        """
        return bool(self.cfg.get("enabled", False))

    def _api_key(self) -> str:
        """读取 API Key。

        Args:
            None

        Returns:
            str: API Key。
        """
        env_name = str(self.cfg.get("api_key_env", "") or "").strip()
        return os.environ.get(env_name, "") if env_name else ""

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用 DeepSeek Chat Completions，并要求输出 JSON。

        Args:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。

        Returns:
            Dict[str, Any]: 结构化 JSON；失败时返回空字典。
        """
        return self.chat_json_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=model_override,
            timeout_seconds=timeout_seconds,
        ).get("data", {})

    def chat_json_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用 DeepSeek Chat Completions，并返回详细状态。"""
        if not self.enabled():
            return {
                "ok": False,
                "data": {},
                "error_type": "disabled",
                "error_message": "provider_disabled",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": "",
            }
        api_key = self._api_key()
        base_url = str(self.cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = str(model_override or self.cfg.get("model", "deepseek-chat") or "deepseek-chat").strip()
        timeout_seconds = int(timeout_seconds or self.cfg.get("timeout_seconds", 90) or 90)
        if not api_key or not model:
            return {
                "ok": False,
                "data": {},
                "error_type": "config_error",
                "error_message": "missing_api_key_or_model",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": model,
            }
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "temperature": float(self.cfg.get("temperature", 0.1) or 0.1),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        start = time.time()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return {
                "ok": isinstance(parsed, dict) and bool(parsed),
                "data": parsed if isinstance(parsed, dict) else {},
                "error_type": "" if parsed else "empty_response",
                "error_message": "" if parsed else "empty_json_payload",
                "status_code": resp.status_code,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
            }
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.text[:1200]
                except Exception:
                    body = ""
            return {
                "ok": False,
                "data": {},
                "error_type": "http_error",
                "error_message": f"{type(exc).__name__}: {exc} | body={body}",
                "status_code": exc.response.status_code if exc.response is not None else None,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
            }
        except Exception as exc:
            return {
                "ok": False,
                "data": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "status_code": None,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
            }


class OpenAIResponsesClient:
    """OpenAI 研究脑客户端，走 Responses API。"""

    def __init__(self, provider_name: str, cfg: Dict[str, Any], schema_root: Optional[Path] = None):
        """初始化客户端。

        Args:
            provider_name: 提供方名称。
            cfg: 提供方配置。
            schema_root: JSON Schema 根目录。

        Returns:
            None
        """
        self.provider_name = provider_name
        self.cfg = dict(cfg or {})
        self.schema_root = schema_root

    def enabled(self) -> bool:
        """判断是否启用。

        Args:
            None

        Returns:
            bool: 是否启用。
        """
        return bool(self.cfg.get("enabled", False))

    def _api_key(self) -> str:
        """读取 API Key。

        Args:
            None

        Returns:
            str: API Key。
        """
        env_name = str(self.cfg.get("api_key_env", "") or "").strip()
        return os.environ.get(env_name, "") if env_name else ""

    def _load_schema(self, schema_name: Optional[str]) -> Optional[Dict[str, Any]]:
        """读取 JSON Schema。

        Args:
            schema_name: Schema 文件名。

        Returns:
            Optional[Dict[str, Any]]: Schema 字典。
        """
        if not schema_name or not self.schema_root:
            return None
        path = self.schema_root / schema_name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _extract_output_text(resp_json: Dict[str, Any]) -> str:
        """从 Responses API 的 HTTP JSON 中提取输出文本。

        Args:
            resp_json: Responses API 返回对象。

        Returns:
            str: 合并后的输出文本。
        """
        output_text = resp_json.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        chunks = []
        for item in resp_json.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text":
                    text = str(content.get("text", "") or "")
                    if text:
                        chunks.append(text)
        return "\n".join(chunks).strip()

    @staticmethod
    def _supports_reasoning_effort(model: str) -> bool:
        """判断模型是否大概率支持 reasoning.effort。"""
        name = str(model or "").strip().lower()
        if not name:
            return False
        return name.startswith(("gpt-5", "o1", "o3", "o4"))

    @staticmethod
    def _is_reasoning_effort_error(status_code: Optional[int], body: str) -> bool:
        text = str(body or "").lower()
        return int(status_code or 0) == 400 and "reasoning.effort" in text and "unsupported parameter" in text

    def create_json_response(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用 OpenAI Responses API，并返回 JSON。

        Args:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            schema_name: JSON Schema 文件名。

        Returns:
            Dict[str, Any]: 结构化 JSON；失败时返回空字典。
        """
        return self.create_json_response_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
        ).get("data", {})

    def create_json_response_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: Optional[str] = None,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用 OpenAI Responses API，并返回详细状态。"""
        if not self.enabled():
            return {
                "ok": False,
                "data": {},
                "error_type": "disabled",
                "error_message": "provider_disabled",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": "",
            }
        api_key = self._api_key()
        base_url = str(self.cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        model = str(model_override or self.cfg.get("model", "gpt-5.4") or "gpt-5.4").strip()
        timeout_seconds = int(timeout_seconds or self.cfg.get("timeout_seconds", 180) or 180)
        schema = self._load_schema(schema_name=schema_name)
        if not api_key or not model:
            return {
                "ok": False,
                "data": {},
                "error_type": "config_error",
                "error_message": "missing_api_key_or_model",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": model,
            }
        url = f"{base_url}/responses"
        payload: Dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "store": bool(self.cfg.get("store", False)),
        }
        effective_reasoning_effort = str(reasoning_effort or self.cfg.get("reasoning_effort", "medium") or "medium").strip()
        if effective_reasoning_effort and self._supports_reasoning_effort(model):
            payload["reasoning"] = {"effort": effective_reasoning_effort}
        if max_output_tokens is not None:
            payload["max_output_tokens"] = int(max_output_tokens)
        if schema:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema.get("title", "structured_response"),
                    "strict": True,
                    "schema": schema,
                }
            }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        start = time.time()
        transient_retry_count = max(int(self.cfg.get("transient_retry_count", 1) or 0), 0)
        attempts_allowed = 1 + transient_retry_count
        last_error: Dict[str, Any] | None = None
        for attempt_idx in range(attempts_allowed):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
                resp.raise_for_status()
                data = resp.json()
                content = self._extract_output_text(data)
                try:
                    parsed = json.loads(content) if content else {}
                except json.JSONDecodeError as exc:
                    incomplete = data.get("incomplete_details") or {}
                    return {
                        "ok": False,
                        "data": {},
                        "error_type": "incomplete_response" if incomplete else type(exc).__name__,
                        "error_message": (
                            f"{type(exc).__name__}: {exc}"
                            + (f" | incomplete_reason={incomplete.get('reason')}" if incomplete else "")
                        ),
                        "status_code": resp.status_code,
                        "elapsed_seconds": round(time.time() - start, 3),
                        "provider": self.provider_name,
                        "model": str(data.get("model", model) or model),
                        "response_id": str(data.get("id", "") or ""),
                        "output_chars": len(content or ""),
                    }
                return {
                    "ok": isinstance(parsed, dict) and bool(parsed),
                    "data": parsed if isinstance(parsed, dict) else {},
                    "error_type": "" if parsed else "empty_response",
                    "error_message": "" if parsed else "empty_output_text",
                    "status_code": resp.status_code,
                    "elapsed_seconds": round(time.time() - start, 3),
                    "provider": self.provider_name,
                    "model": str(data.get("model", model) or model),
                    "response_id": str(data.get("id", "") or ""),
                    "output_chars": len(content or ""),
                }
            except requests.HTTPError as exc:
                body = ""
                if exc.response is not None:
                    try:
                        body = exc.response.text[:2000]
                    except Exception:
                        body = ""
                status_code = exc.response.status_code if exc.response is not None else None
                if self._is_reasoning_effort_error(status_code=status_code, body=body) and "reasoning" in payload:
                    payload.pop("reasoning", None)
                    continue
                last_error = {
                    "ok": False,
                    "data": {},
                    "error_type": "http_error",
                    "error_message": f"{type(exc).__name__}: {exc} | body={body}",
                    "status_code": status_code,
                    "elapsed_seconds": round(time.time() - start, 3),
                    "provider": self.provider_name,
                    "model": model,
                    "response_id": "",
                    "output_chars": 0,
                }
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = {
                    "ok": False,
                    "data": {},
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "status_code": None,
                    "elapsed_seconds": round(time.time() - start, 3),
                    "provider": self.provider_name,
                    "model": model,
                    "response_id": "",
                    "output_chars": 0,
                }
                if attempt_idx + 1 < attempts_allowed:
                    time.sleep(min(1.5 * (attempt_idx + 1), 3.0))
                    continue
                break
            except Exception as exc:
                last_error = {
                    "ok": False,
                    "data": {},
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "status_code": None,
                    "elapsed_seconds": round(time.time() - start, 3),
                    "provider": self.provider_name,
                    "model": model,
                    "response_id": "",
                    "output_chars": 0,
                }
                break
        return last_error or {
            "ok": False,
            "data": {},
            "error_type": "unknown_error",
            "error_message": "unexpected_openai_failure",
            "status_code": None,
            "elapsed_seconds": round(time.time() - start, 3),
            "provider": self.provider_name,
            "model": model,
            "response_id": "",
            "output_chars": 0,
        }


class LocalOllamaChatClient:
    """本地 Ollama 通用 JSON 客户端。"""

    def __init__(self, provider_name: str, cfg: Dict[str, Any]):
        self.provider_name = provider_name
        self.cfg = dict(cfg or {})

    def enabled(self) -> bool:
        """判断是否启用。"""
        if "research_enabled" in self.cfg:
            return bool(self.cfg.get("research_enabled", False))
        return bool(self.cfg)

    def chat_json_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用本地 Ollama，并返回详细状态。"""
        if not self.enabled():
            return {
                "ok": False,
                "data": {},
                "error_type": "disabled",
                "error_message": "provider_disabled",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": "",
            }
        base_url = str(self.cfg.get("base_url", "http://localhost:11434")).rstrip("/")
        model = str(model_override or self.cfg.get("model", "qwen2.5:7b") or "qwen2.5:7b").strip()
        timeout_seconds = int(timeout_seconds or self.cfg.get("research_timeout_seconds", self.cfg.get("timeout_seconds", 120)) or 120)
        cooldown_seconds = max(int(self.cfg.get("cooldown_seconds", 45) or 45), 5)
        health_timeout_seconds = min(float(self.cfg.get("health_timeout_seconds", 1.5) or 1.5), max(timeout_seconds / 4.0, 0.5))
        cache_key = _ollama_cache_key(base_url, model)
        if _ollama_in_cooldown(cache_key):
            return {
                "ok": False,
                "data": {},
                "error_type": "service_cooldown",
                "error_message": "ollama_recent_timeout_cooldown",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": 0,
            }
        if bool(self.cfg.get("healthcheck_enabled", True)) and not _ollama_ready(base_url, timeout_seconds=health_timeout_seconds):
            _mark_ollama_cooldown(cache_key, cooldown_seconds=max(cooldown_seconds // 2, 10))
            return {
                "ok": False,
                "data": {},
                "error_type": "service_unavailable",
                "error_message": "ollama_healthcheck_failed",
                "status_code": None,
                "elapsed_seconds": 0.0,
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": 0,
            }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        start = time.time()
        try:
            resp = requests.post(f"{base_url}/api/chat", json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            content = str(data.get("message", {}).get("content", "") or "").strip()
            parsed = parse_json_object_loose(content) if content else {}
            return {
                "ok": isinstance(parsed, dict) and bool(parsed),
                "data": parsed if isinstance(parsed, dict) else {},
                "error_type": "" if parsed else "empty_response",
                "error_message": "" if parsed else "empty_output_text",
                "status_code": resp.status_code,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": len(content or ""),
            }
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.text[:1200]
                except Exception:
                    body = ""
            return {
                "ok": False,
                "data": {},
                "error_type": "http_error",
                "error_message": f"{type(exc).__name__}: {exc} | body={body}",
                "status_code": exc.response.status_code if exc.response is not None else None,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": 0,
            }
        except (requests.Timeout, requests.ConnectionError) as exc:
            _mark_ollama_cooldown(cache_key, cooldown_seconds=cooldown_seconds)
            _OLLAMA_HEALTH_CACHE[base_url] = {"ok": False, "expires_at": time.time() + max(cooldown_seconds // 2, 10)}
            return {
                "ok": False,
                "data": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "status_code": None,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": 0,
            }
        except Exception as exc:
            return {
                "ok": False,
                "data": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "status_code": None,
                "elapsed_seconds": round(time.time() - start, 3),
                "provider": self.provider_name,
                "model": model,
                "response_id": "",
                "output_chars": 0,
            }


class LLMRouter:
    """按职责分发到 DeepSeek 与 GPT-5.4。"""

    def __init__(
        self,
        provider_cfg: Dict[str, Any],
        schema_root: Optional[Path] = None,
        local_ollama_cfg: Optional[Dict[str, Any]] = None,
    ):
        """初始化路由器。

        Args:
            provider_cfg: providers 配置。
            schema_root: JSON Schema 根目录。

        Returns:
            None
        """
        provider_cfg = dict(provider_cfg or {})
        self.deepseek = DeepSeekChatClient(
            provider_name="deepseek_worker",
            cfg=provider_cfg.get("deepseek_worker", {}),
        )
        self.openai = OpenAIResponsesClient(
            provider_name="openai_research",
            cfg=provider_cfg.get("openai_research", {}),
            schema_root=schema_root,
        )
        self.local_ollama = LocalOllamaChatClient(
            provider_name="local_ollama_research",
            cfg=local_ollama_cfg or {},
        )

    def call_worker_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用低成本执行脑。

        Args:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。

        Returns:
            Dict[str, Any]: 结构化输出。
        """
        return self.deepseek.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=model_override,
            timeout_seconds=timeout_seconds,
        )

    def call_worker_json_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用低成本执行脑，并返回详细状态。"""
        return self.deepseek.chat_json_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=model_override,
            timeout_seconds=timeout_seconds,
        )

    def call_research_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用高价值研究脑。

        Args:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            schema_name: JSON Schema 文件名。

        Returns:
            Dict[str, Any]: 结构化输出。
        """
        return self.openai.create_json_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
        )

    def call_research_json_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: Optional[str] = None,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用高价值研究脑，并返回详细状态。"""
        return self.openai.create_json_response_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
            model_override=model_override,
            timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )

    def call_local_json_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用本地 Ollama，并返回详细状态。"""
        return self.local_ollama.chat_json_detailed(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=model_override,
            timeout_seconds=timeout_seconds,
        )
