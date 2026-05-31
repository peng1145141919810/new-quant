# -*- coding: utf-8 -*-
"""研究流水线主控编排（ingest → bridge 分段）。"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

from .config_utils import load_config
from .context_pack import build_research_context_pack, save_research_context_pack
from .data_gap_engine import build_data_gap_report, save_data_gap_report
from .data_inventory import build_inventory_real, save_inventory
from .event_extract import extract_events_with_worker, save_event_store
from .event_ingest import ingest_events_real, refresh_market_basics
from .industry_router import build_industry_router_artifacts
from .integrated_thesis import build_integrated_thesis_artifacts
from .local_augmentations import build_announcement_evidence_cards, build_manual_review_queue
from .logging_utils import log_line
from .market_state import build_market_state_artifacts
from .research_brief_engine import build_research_brief, save_research_brief
from .research_bridge import build_research_actions, save_bridge_outputs


def _load_research_meta_feedback(config: dict) -> dict:
    path_text = str(config.get("oms", {}).get("output_root", "") or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).resolve() / "feedback" / "research_meta_feedback_latest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _research_cycle_stage_labels(mode: str) -> list[str]:
    labels: list[str] = []
    if mode in {"ingest_only", "extract_only", "gap_only", "plan_only", "bridge_only", "full_cycle"}:
        labels.append("基础表刷新与事件抓取")
    if mode in {"extract_only", "gap_only", "plan_only", "bridge_only", "full_cycle"}:
        labels.append("事件抽取")
    if mode in {"industry_router_only", "plan_only", "bridge_only", "full_cycle"}:
        labels.append("行业研究骨架")
    if mode in {"plan_only", "bridge_only", "full_cycle"}:
        labels.append("市场状态与资金面总阀门")
    if mode in {"gap_only", "plan_only", "bridge_only", "full_cycle"}:
        labels.append("数据清单与缺口分析")
    if mode in {"plan_only", "bridge_only", "full_cycle"}:
        labels.append("研究证据包与研究计划")
    if mode in {"bridge_only", "full_cycle"}:
        labels.append("桥接产物生成")
    return labels


def run_research_cycle(config_path: Path, mode: str = "full_cycle") -> None:
    """运行单轮研究编排（按 mode 选择阶段）。"""
    config = load_config(config_path=config_path)
    project_root = config_path.resolve().parent.parent
    prompt_root = project_root / "prompts"
    stage_labels = _research_cycle_stage_labels(mode=mode)
    log_line(config, f"研究流水线: 进入单轮编排，mode={mode} stages={len(stage_labels)}")

    raw_items = []
    structured_events = []
    evidence_cards = []
    industry_router_result = {}
    inventory = {}
    data_gap_report = {}
    context_pack = {}
    research_brief = {}
    stage_idx = 0
    industry_router_payload = {}
    market_state_payload = {}
    integrated_thesis_payload = {}

    if mode in {"ingest_only", "extract_only", "gap_only", "plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 基础表刷新与事件抓取开始")
        refresh_market_basics(config=config)
        raw_items = ingest_events_real(config=config)
        evidence_result = build_announcement_evidence_cards(config=config, raw_items=raw_items)
        evidence_cards = list(evidence_result.get("cards", []) or [])
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 公告证据卡摘要 selected={evidence_result.get('selected_items', 0)} "
            f"cards={len(evidence_cards)} path={evidence_result.get('path', '')}",
        )
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 基础表刷新与事件抓取完成 raw_items={len(raw_items)} elapsed={perf_counter() - t0:.1f}s")

    if mode in {"extract_only", "gap_only", "plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 事件抽取开始 input_raw_items={len(raw_items)}")
        structured_events = extract_events_with_worker(
            config=config,
            raw_items=raw_items,
            prompt_root=prompt_root,
        )
        save_event_store(config=config, events=structured_events)
        review_result = build_manual_review_queue(config=config, structured_events=structured_events)
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 人工复核队列摘要 queue_size={review_result.get('queue_size', 0)} "
            f"path={review_result.get('path', '')}",
        )
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 事件抽取完成 structured_events={len(structured_events)} elapsed={perf_counter() - t0:.1f}s")

    if mode in {"industry_router_only", "plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 分行业研究骨架开始")
        industry_router_result = build_industry_router_artifacts(config=config, structured_events=structured_events)
        if bool(config.get("industry_router", {}).get("enable_context_pack", True)):
            industry_router_payload = dict(industry_router_result.get("context_payload", {}) or {})
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 分行业研究骨架完成 "
            f"thesis_count={industry_router_result.get('summary', {}).get('thesis_count', 0)} "
            f"signal_rows={industry_router_result.get('summary', {}).get('signal_rows', 0)} "
            f"summary={industry_router_result.get('summary_path', '')} elapsed={perf_counter() - t0:.1f}s",
        )

    if mode in {"plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 市场状态与资金面总阀门开始")
        market_state_result = build_market_state_artifacts(config=config)
        market_state_payload = dict(market_state_result.get("payload", {}) or {})
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 市场状态与资金面总阀门完成 "
            f"regime={market_state_payload.get('market_regime', '')} "
            f"style={market_state_payload.get('style_bias', '')} "
            f"mechanism={market_state_payload.get('mechanism_bias', '')} "
            f"elapsed={perf_counter() - t0:.1f}s",
        )
        integrated_result = build_integrated_thesis_artifacts(
            config=config,
            structured_events=structured_events,
            industry_router_payload=industry_router_payload,
            market_state_payload=market_state_payload,
        )
        integrated_thesis_payload = dict(integrated_result.get("payload", {}) or {})
        log_line(
            config,
            f"研究流水线: integrated_thesis refreshed primary={integrated_thesis_payload.get('primary_strategy_key', '')} "
            f"symbols={dict(integrated_thesis_payload.get('summary', {}) or {}).get('n_symbols', 0)} "
            f"path={integrated_result.get('latest_path', '')}",
        )

    if mode in {"gap_only", "plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 数据清单与缺口分析开始")
        inventory = build_inventory_real(config=config)
        save_inventory(config=config, inventory=inventory)
        data_gap_report = build_data_gap_report(
            config=config,
            inventory=inventory,
            structured_events=structured_events,
        )
        save_data_gap_report(config=config, report=data_gap_report)
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 数据清单与缺口分析完成 datasets={len(list(inventory.get('datasets', []) or []))} "
            f"refresh_tasks={len(list(data_gap_report.get('refresh_tasks', []) or []))} "
            f"recompute_tasks={len(list(data_gap_report.get('recompute_tasks', []) or []))} elapsed={perf_counter() - t0:.1f}s",
        )

    if mode in {"plan_only", "bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 研究证据包与研究计划开始")
        context_pack = build_research_context_pack(
            config=config,
            structured_events=structured_events,
            data_gap_report=data_gap_report,
            evidence_cards=evidence_cards,
            industry_router_payload=industry_router_payload,
            market_state_payload=market_state_payload,
            integrated_thesis_payload=integrated_thesis_payload,
            research_meta_feedback=_load_research_meta_feedback(config=config),
        )
        save_research_context_pack(config=config, pack=context_pack)
        log_line(config, f"研究流水线: 研究证据包已合并公告证据卡 evidence_cards={len(list(context_pack.get('evidence_cards', []) or []))}")
        research_brief = build_research_brief(
            config=config,
            context_pack=context_pack,
            prompt_root=prompt_root,
        )
        save_research_brief(config=config, brief=research_brief)
        log_line(
            config,
            "研究计划生成完成，mode=%s provider=%s model=%s theses=%s candidate_experiments=%s"
            % (
                research_brief.get("generation_mode", "unknown"),
                research_brief.get("llm_provider", ""),
                research_brief.get("llm_model", ""),
                len(list(research_brief.get("core_theses", []) or [])),
                len(list(research_brief.get("candidate_experiments", []) or [])),
            ),
        )
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 研究证据包与研究计划完成 elapsed={perf_counter() - t0:.1f}s")

    if mode in {"bridge_only", "full_cycle"}:
        stage_idx += 1
        t0 = perf_counter()
        log_line(config, f"研究流水线: [{stage_idx}/{len(stage_labels)}] 桥接产物生成开始")
        actions = build_research_actions(brief=research_brief)
        save_bridge_outputs(config=config, actions=actions)
        log_line(
            config,
            f"研究流水线: [{stage_idx}/{len(stage_labels)}] 桥接产物生成完成 feature_profiles={len(list(actions.get('candidate_override', {}).get('feature_profiles', []) or []))} "
            f"label_horizons={len(list(actions.get('candidate_override', {}).get('label_horizons', []) or []))} elapsed={perf_counter() - t0:.1f}s",
        )
