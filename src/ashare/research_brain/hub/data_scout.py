# -*- coding: utf-8 -*-
"""新数据侦察。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import requests

from hub.io_utils import write_json


def scout_data_sources(config: Dict[str, Any], cycle_dir: Path) -> Dict[str, Any]:
    """抓取候选网页，给研究脑留下新数据线索。

    Args:
        config: 全局配置。
        cycle_dir: 当前轮次目录。

    Returns:
        侦察结果。
    """
    scout_cfg = dict(config.get('data_scout', {}))
    if not bool(scout_cfg.get('enabled', False)):
        payload = {'enabled': False, 'records': []}
        write_json(cycle_dir / 'data_scout.json', payload)
        return payload

    urls = list(scout_cfg.get('candidate_urls', []))
    max_chars = int(scout_cfg.get('max_chars_per_page', 5000) or 5000)
    records: List[Dict[str, Any]] = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            text = resp.text[:max_chars]
            records.append({'url': url, 'ok': True, 'text_preview': text})
        except Exception as exc:
            records.append({'url': url, 'ok': False, 'error': str(exc)})
    payload = {'enabled': True, 'records': records}
    write_json(cycle_dir / 'data_scout.json', payload)
    return payload
