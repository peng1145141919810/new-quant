# -*- coding: utf-8 -*-
"""V6 数据缺口引擎。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .config_utils import ensure_dir


def build_data_gap_report(
    config: Dict[str, Any],
    inventory: Dict[str, Any],
    structured_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """根据数据清单与事件摘要生成缺口报告。

    Args:
        config: V6 配置。
        inventory: 数据清单。
        structured_events: 结构化事件列表。

    Returns:
        Dict[str, Any]: 数据缺口报告。
    """
    refresh_tasks: List[Dict[str, Any]] = []
    recompute_tasks: List[Dict[str, Any]] = []
    new_feature_candidates: List[Dict[str, Any]] = []

    for item in inventory.get("datasets", []):
        status = str(item.get("freshness_status", "unknown"))
        dataset_name = str(item.get("dataset_name", ""))
        if status in {"stale", "missing"}:
            refresh_tasks.append(
                {
                    "task_id": f"refresh_{dataset_name}_{datetime.now().strftime('%Y%m%d')}",
                    "task_type": "refresh_dataset",
                    "priority": "high" if status == "missing" else "medium",
                    "reason": f"{dataset_name} 当前状态为 {status}",
                    "dataset_name": dataset_name,
                }
            )

    earnings_like = [
        e for e in structured_events
        if str(e.get("event_type", "")) in {"earnings_preannounce", "earnings_flash", "financial_report"}
    ]
    if earnings_like:
        recompute_tasks.append(
            {
                "task_id": f"recompute_earnings_surprise_{datetime.now().strftime('%Y%m%d')}",
                "task_type": "recompute_feature",
                "priority": "high",
                "reason": "近期业绩相关事件出现，建议重算事件型业绩特征",
                "feature_name": "earnings_surprise_proxy",
                "trigger_event_id": str(earnings_like[0].get("event_id", "")),
            }
        )
        new_feature_candidates.append(
            {
                "feature_name": "earnings_event_density_20d",
                "priority": "high",
                "reason": "近期业绩事件簇出现，值得测试事件密度特征",
            }
        )

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": "由数据清单与事件触发生成的缺口报告。",
        "datasets": [
            {
                "dataset_name": str(item.get("dataset_name", "")),
                "freshness_status": str(item.get("freshness_status", "")),
                "missing_ratio": float(item.get("missing_ratio", 0.0) or 0.0),
                "action": "refresh" if str(item.get("freshness_status", "")) in {"stale", "missing"} else "none",
            }
            for item in inventory.get("datasets", [])
        ],
        "refresh_tasks": refresh_tasks,
        "recompute_tasks": recompute_tasks,
        "new_feature_candidates": new_feature_candidates,
    }
    return report


def save_data_gap_report(config: Dict[str, Any], report: Dict[str, Any]) -> Path:
    """保存数据缺口报告。

    Args:
        config: V6 配置。
        report: 数据缺口报告。

    Returns:
        Path: 输出路径。
    """
    root = Path(str(config["paths"]["inventory_root"]))
    ensure_dir(root)
    out_path = root / "data_gap_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
