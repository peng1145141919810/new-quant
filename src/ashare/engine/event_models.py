# -*- coding: utf-8 -*-
"""V6 事件与研究对象的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EventRecord:
    """结构化事件记录。"""

    event_id: str
    publish_time: str
    source_type: str
    source_name: str
    event_type: str
    event_direction: str
    impact_scope: str
    impact_horizon: str
    confidence: float
    importance_score: float
    raw_title: str
    raw_text: str
    extract_model: str
    crawl_time: Optional[str] = None
    security_code: Optional[str] = None
    company_name: Optional[str] = None
    industry_tags: List[str] = field(default_factory=list)
    novelty_score: Optional[float] = None
    structured_facts: Dict[str, Any] = field(default_factory=dict)
    review_status: str = "auto_approved"


@dataclass
class RefreshTask:
    """数据刷新或重算任务。"""

    task_id: str
    task_type: str
    priority: str
    reason: str
    dataset_name: Optional[str] = None
    feature_name: Optional[str] = None
    trigger_event_id: Optional[str] = None
