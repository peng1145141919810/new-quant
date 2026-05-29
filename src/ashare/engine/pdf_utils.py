# -*- coding: utf-8 -*-
"""PDF 下载与文本抽取。"""

from __future__ import annotations

from pathlib import Path

import requests
from pypdf import PdfReader


def download_pdf(url: str, out_path: Path, timeout_seconds: int = 60) -> bool:
    """下载 PDF。

    Args:
        url: 下载地址。
        out_path: 输出路径。
        timeout_seconds: 超时秒数。

    Returns:
        bool: 是否成功。
    """
    try:
        resp = requests.get(url, timeout=timeout_seconds)
        resp.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return True
    except Exception:
        return False


def extract_pdf_text(pdf_path: Path, max_chars: int = 30000) -> str:
    """抽取 PDF 文本。

    Args:
        pdf_path: PDF 路径。
        max_chars: 最大字符数。

    Returns:
        str: 抽取文本。
    """
    try:
        reader = PdfReader(str(pdf_path))
        chunks = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                remaining = max_chars - total
                if remaining <= 0:
                    break
                text = text[:remaining]
                chunks.append(text)
                total += len(text)
        return "\n".join(chunks).strip()
    except Exception:
        return ""
