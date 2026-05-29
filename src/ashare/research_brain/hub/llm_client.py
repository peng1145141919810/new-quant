# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端。

为了同时兼容 OpenAI 与 DeepSeek，这里使用 openai-compatible HTTP 接口，
不强依赖某一家官方 SDK。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import requests


class LLMClient:
    """轻量 LLM 客户端。"""

    def __init__(self, cfg: Dict[str, Any]):
        """初始化。

        Args:
            cfg: llm_brain 配置。

        Returns:
            None
        """
        self.cfg = dict(cfg or {})

    def is_enabled(self) -> bool:
        """是否启用。

        Args:
            None

        Returns:
            bool
        """
        return bool(self.cfg.get('enabled', False))

    def _api_key(self) -> Optional[str]:
        """取 API key。

        Args:
            None

        Returns:
            key 或 None
        """
        env_name = str(self.cfg.get('api_key_env', '') or '').strip()
        if not env_name:
            return None
        return os.environ.get(env_name)

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> Dict[str, Any]:
        """请求结构化 JSON。

        Args:
            system_prompt: 系统提示。
            user_prompt: 用户提示。
            temperature: 温度。

        Returns:
            JSON 字典。
        """
        if not self.is_enabled():
            return {}
        api_key = self._api_key()
        if not api_key:
            return {}
        base_url = str(self.cfg.get('base_url', '')).rstrip('/')
        model = str(self.cfg.get('model', '') or '').strip()
        timeout_seconds = int(self.cfg.get('timeout_seconds', 120) or 120)
        if not base_url or not model:
            return {}
        url = f'{base_url}/chat/completions'
        payload = {
            'model': model,
            'temperature': temperature,
            'response_format': {'type': 'json_object'},
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
        }
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            content = data['choices'][0]['message']['content']
            return json.loads(content)
        except Exception:
            return {}

    def chat_text(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        """请求文本。

        Args:
            system_prompt: 系统提示。
            user_prompt: 用户提示。
            temperature: 温度。

        Returns:
            模型文本。
        """
        if not self.is_enabled():
            return ''
        api_key = self._api_key()
        if not api_key:
            return ''
        base_url = str(self.cfg.get('base_url', '')).rstrip('/')
        model = str(self.cfg.get('model', '') or '').strip()
        timeout_seconds = int(self.cfg.get('timeout_seconds', 120) or 120)
        if not base_url or not model:
            return ''
        url = f'{base_url}/chat/completions'
        payload = {
            'model': model,
            'temperature': temperature,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
        }
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            return str(data['choices'][0]['message']['content'])
        except Exception:
            return ''
