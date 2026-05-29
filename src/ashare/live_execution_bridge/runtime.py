from __future__ import annotations

from typing import Any, Dict

from engine.oms.runtime import run_oms_cycle


def run_once(config: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次桥接运行，实际 truth/reconcile 由 OMS 负责。"""
    return run_oms_cycle(config)
