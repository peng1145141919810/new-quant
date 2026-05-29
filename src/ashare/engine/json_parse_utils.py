# -*- coding: utf-8 -*-
"""Loose JSON parsing helpers for LLM outputs."""

from __future__ import annotations

import json
from typing import Any, Dict


def _strip_fence(text: str) -> str:
    raw = str(text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    raw = "\n".join(lines).strip()
    if raw.lower().startswith("json\n"):
        raw = raw.split("\n", 1)[1].strip()
    return raw


def _first_json_object(text: str) -> str:
    raw = str(text or "")
    for start_idx, ch in enumerate(raw):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end_idx in range(start_idx, len(raw)):
            token = raw[end_idx]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if token == "\\":
                    escaped = True
                    continue
                if token == '"':
                    in_string = False
                continue
            if token == '"':
                in_string = True
                continue
            if token == "{":
                depth += 1
                continue
            if token == "}":
                depth -= 1
                if depth == 0:
                    return raw[start_idx : end_idx + 1]
    return ""


def parse_json_object_loose(text: Any) -> Dict[str, Any]:
    """Parse an LLM response that may wrap a JSON object with extra text."""
    raw = _strip_fence(str(text or "").strip())
    if not raw:
        return {}
    for candidate in (raw, _first_json_object(raw)):
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
