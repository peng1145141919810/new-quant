# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from . import local_settings as LS


def _dedupe_nonempty(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding=encoding)
    os.replace(tmp_path, path)
    return path

def build_runtime_config() -> Dict[str, Any]:
    enabled_sources = []
    if LS.ENABLE_CNINFO or LS.ENABLE_SSE or LS.ENABLE_SZSE:
        enabled_sources.append("announcements")
    if LS.ENABLE_TUSHARE_NEWS or LS.ENABLE_TUSHARE_MAJOR_NEWS:
        enabled_sources.append("news")
    ollama_event_extract_model = str(getattr(LS, "OLLAMA_EVENT_EXTRACT_MODEL", getattr(LS, "OLLAMA_MODEL", "qwen2.5:7b")) or "qwen2.5:7b")
    ollama_event_extract_timeout_seconds = int(getattr(LS, "OLLAMA_EVENT_EXTRACT_TIMEOUT_SECONDS", getattr(LS, "OLLAMA_TIMEOUT_SECONDS", 120)) or 120)
    ollama_research_model = str(getattr(LS, "OLLAMA_RESEARCH_MODEL", ollama_event_extract_model) or ollama_event_extract_model)
    ollama_research_models = _dedupe_nonempty(
        [ollama_research_model] + list(getattr(LS, "OLLAMA_RESEARCH_FALLBACK_MODELS", []) or [])
    ) or [ollama_research_model]
    ollama_research_timeout_seconds = int(getattr(LS, "OLLAMA_RESEARCH_TIMEOUT_SECONDS", getattr(LS, "OLLAMA_TIMEOUT_SECONDS", 120)) or 120)
    ollama_evidence_card_model = str(getattr(LS, "OLLAMA_EVIDENCE_CARD_MODEL", ollama_research_model) or ollama_research_model)
    ollama_review_router_model = str(getattr(LS, "OLLAMA_REVIEW_ROUTER_MODEL", ollama_event_extract_model) or ollama_event_extract_model)
    ollama_runtime_explainer_model = str(getattr(LS, "OLLAMA_RUNTIME_EXPLAINER_MODEL", ollama_event_extract_model) or ollama_event_extract_model)
    ollama_v5_review_model = str(getattr(LS, "OLLAMA_V5_REVIEW_MODEL", ollama_research_model) or ollama_research_model)
    cfg: Dict[str, Any] = {
        "project_name": "quant_research_engine_lean_portfolio_integrated",
        "project_root": str(LS.PROJECT_ROOT),
        "train_table_dir": LS.TRAIN_TABLE_DIR,
        "hub_output_root": LS.HUB_OUTPUT_ROOT,
        "execution": {"python_executable": LS.PYTHON_EXECUTABLE, "mode": LS.RUN_MODE, "max_cycles": 1},
        "paths": {
            "raw_event_root": LS.RAW_EVENT_ROOT,
            "event_store_root": LS.EVENT_STORE_ROOT,
            "inventory_root": LS.INVENTORY_ROOT,
            "research_root": LS.RESEARCH_ROOT,
            "bridge_root": LS.BRIDGE_ROOT,
            "market_state_root": LS.MARKET_STATE_ROOT,
            "daily_cache_root": LS.DAILY_CACHE_ROOT,
            "log_root": LS.LOG_ROOT,
            "portfolio_output_root": LS.PORTFOLIO_OUTPUT_ROOT,
            "live_execution_root": LS.LIVE_EXECUTION_ROOT,
            "live_price_snapshot_path": str(getattr(LS, "LIVE_PRICE_SNAPSHOT_PATH", Path(LS.LIVE_EXECUTION_ROOT) / "daily_price_snapshot.csv")),
            "trade_release_root": str(getattr(LS, "TRADE_RELEASE_ROOT", Path(LS.LIVE_EXECUTION_ROOT).parents[0] / "trade_release")),
            "trade_clock_root": str(getattr(LS, "TRADE_CLOCK_ROOT", Path(LS.LIVE_EXECUTION_ROOT).parents[0] / "trade_clock")),
            "automation_runs_root": str(getattr(LS, "AUTOMATION_RUNS_ROOT", Path(LS.PROJECT_ROOT).parents[1] / "outputs" / "automation_runs")),
            "trading_calendar_cache_path": str(getattr(LS, "TRADING_CALENDAR_CACHE_PATH", Path(LS.MARKET_STATE_ROOT) / "trading_calendar_a_share.csv")),
            "industry_router_output_root": str(getattr(LS, "INDUSTRY_ROUTER_OUTPUT_ROOT", Path(LS.RESEARCH_ROOT) / "industry_router")),
            "technical_confirmation_root": str(getattr(LS, "TECHNICAL_CONFIRMATION_ROOT", Path(LS.RESEARCH_ROOT) / "technical_confirmation")),
            "oms_output_root": str(getattr(LS, "OMS_OUTPUT_ROOT", Path(LS.LIVE_EXECUTION_ROOT) / "oms_v1")),
            "affordable_sqlite_path": str(getattr(LS, "AFFORDABLE_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "affordable_data_v1.sqlite3")),
            "research_fact_sqlite_path": str(getattr(LS, "RESEARCH_FACT_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_fact_layers_v1.sqlite3")),
            "affordable_snapshot_root": str(getattr(LS, "AFFORDABLE_SNAPSHOT_ROOT", Path(LS.DATA_ROOT) / "affordable_feeds" / "latest")),
            "manual_event_proxy_path": str(getattr(LS, "MANUAL_EVENT_PROXY_PATH", Path(LS.RESEARCH_ROOT) / "manual_event_proxy" / "manual_event_proxy.jsonl")),
            "external_research_root": str(getattr(LS, "EXTERNAL_RESEARCH_ROOT", Path(LS.DATA_ROOT) / "external_research_feeds" / "latest")),
            "data_sqlite_path": str(
                getattr(LS, "RUNTIME_DATA_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_data_v1.sqlite3")
            ),
        },
        "data_store": {
            "enabled": bool(getattr(LS, "ENABLE_RUNTIME_DATA_SQL_STORE", True)),
            "sqlite_path": str(
                getattr(LS, "RUNTIME_DATA_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_data_v1.sqlite3")
            ),
            "prefer_sql_for_router": bool(getattr(LS, "RUNTIME_DATA_SQL_PREFER_FOR_ROUTER", False)),
        },
        "providers": {
            "tushare": {
                "enabled": True,
                "token_env": LS.TUSHARE_TOKEN_ENV,
                "token": LS.TUSHARE_TOKEN,
                "rate_limit_sleep_seconds": 0.8,
                "max_retry": 3,
                "retry_sleep_seconds": 2.0,
                "rate_limit_backoff_seconds": 12.0,
            },
            "deepseek_worker": {
                "enabled": LS.ENABLE_DEEPSEEK_WORKER,
                "api_key_env": LS.DEEPSEEK_API_KEY_ENV,
                "base_url": LS.DEEPSEEK_BASE_URL,
                "model": LS.DEEPSEEK_MODEL,
                "research_models": LS.DEEPSEEK_RESEARCH_FALLBACK_MODELS,
                "temperature": 0.1,
                "timeout_seconds": 90,
            },
            "openai_research": {
                "enabled": LS.ENABLE_OPENAI_RESEARCH,
                "api_key_env": LS.OPENAI_API_KEY_ENV,
                "base_url": LS.OPENAI_BASE_URL,
                "model": LS.OPENAI_MODEL,
                "fallback_models": LS.OPENAI_RESEARCH_FALLBACK_MODELS,
                "timeout_seconds": 180,
                "reasoning_effort": "medium",
                "store": False,
            },
        },
        "local_ollama": {
            "research_enabled": LS.ENABLE_LOCAL_OLLAMA_RESEARCH,
            "base_url": LS.OLLAMA_BASE_URL,
            "model": ollama_research_model,
            "research_models": ollama_research_models,
            "timeout_seconds": ollama_event_extract_timeout_seconds,
            "research_timeout_seconds": ollama_research_timeout_seconds,
            "event_extract_model": ollama_event_extract_model,
            "event_extract_timeout_seconds": ollama_event_extract_timeout_seconds,
            "evidence_card_enabled": bool(getattr(LS, "ENABLE_LOCAL_OLLAMA_EVIDENCE_CARD", True)),
            "evidence_card_model": ollama_evidence_card_model,
            "evidence_card_timeout_seconds": int(getattr(LS, "OLLAMA_EVIDENCE_CARD_TIMEOUT_SECONDS", ollama_research_timeout_seconds) or ollama_research_timeout_seconds),
            "evidence_card_max_items": int(getattr(LS, "OLLAMA_EVIDENCE_CARD_MAX_ITEMS", 2) or 2),
            "review_router_enabled": bool(getattr(LS, "ENABLE_LOCAL_OLLAMA_REVIEW_ROUTER", True)),
            "review_router_model": ollama_review_router_model,
            "review_router_timeout_seconds": int(getattr(LS, "OLLAMA_REVIEW_ROUTER_TIMEOUT_SECONDS", ollama_event_extract_timeout_seconds) or ollama_event_extract_timeout_seconds),
            "review_router_max_items": int(getattr(LS, "OLLAMA_REVIEW_ROUTER_MAX_ITEMS", 6) or 6),
            "runtime_explainer_enabled": bool(getattr(LS, "ENABLE_LOCAL_OLLAMA_RUNTIME_EXPLAINER", True)),
            "runtime_explainer_model": ollama_runtime_explainer_model,
            "runtime_explainer_timeout_seconds": int(getattr(LS, "OLLAMA_RUNTIME_EXPLAINER_TIMEOUT_SECONDS", 45) or 45),
            "runtime_explainer_stages": list(getattr(LS, "OLLAMA_RUNTIME_EXPLAINER_STAGES", ["research_plan", "gpu_research", "portfolio_recommendation", "execution_bridge"]) or ["research_plan", "gpu_research", "portfolio_recommendation", "execution_bridge"]),
            "v5_review_enabled": bool(getattr(LS, "ENABLE_LOCAL_OLLAMA_V5_REVIEW", True)),
            "v5_review_model": ollama_v5_review_model,
            "v5_review_timeout_seconds": int(getattr(LS, "OLLAMA_V5_REVIEW_TIMEOUT_SECONDS", ollama_research_timeout_seconds) or ollama_research_timeout_seconds),
        },
        "eastmoney": {
            "enabled": bool(getattr(LS, "ENABLE_EASTMONEY_INTRADAY_KLINE", True)),
            "intraday_kline_base_url": str(getattr(LS, "EASTMONEY_INTRADAY_KLINE_BASE_URL", "https://push2his.eastmoney.com/api/qt/stock/kline/get") or "https://push2his.eastmoney.com/api/qt/stock/kline/get"),
            "timeout_seconds": float(getattr(LS, "EASTMONEY_TIMEOUT_SECONDS", 8.0) or 8.0),
            "sleep_seconds": float(getattr(LS, "EASTMONEY_SLEEP_SECONDS", 0.15) or 0.15),
            "max_retry": int(getattr(LS, "EASTMONEY_MAX_RETRY", 2) or 2),
        },
        "llm_trace": {
            "enabled": bool(getattr(LS, "ENABLE_LLM_TRACE", True)),
            "artifact_root": str(getattr(LS, "LLM_TRACE_ARTIFACT_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "llm_trace")),
            "prompt_char_cap": int(getattr(LS, "LLM_TRACE_PROMPT_CHAR_CAP", 4000) or 4000),
            "raw_response_char_cap": int(getattr(LS, "LLM_TRACE_RAW_RESPONSE_CHAR_CAP", 4000) or 4000),
            "store_prompts": bool(getattr(LS, "LLM_TRACE_STORE_PROMPTS", True)),
            "store_raw_response": bool(getattr(LS, "LLM_TRACE_STORE_RAW_RESPONSE", True)),
        },
        "llm_operating_brain": {
            "enabled": bool(getattr(LS, "ENABLE_LLM_OPERATING_BRAIN", True)),
            "provider": str(getattr(LS, "LLM_OPERATING_BRAIN_PROVIDER", "deepseek_worker") or "deepseek_worker").strip(),
            "timeout_seconds": int(getattr(LS, "LLM_OPERATING_BRAIN_TIMEOUT_SECONDS", 45) or 45),
            "max_candidate_items": int(getattr(LS, "LLM_OPERATING_BRAIN_MAX_CANDIDATE_ITEMS", 10) or 10),
            "artifact_root": str(getattr(LS, "LLM_OPERATING_BRAIN_ARTIFACT_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "llm_operating_brain")),
        },
        "event_ingest": {
            "enabled_sources": enabled_sources,
            "lookback_hours": LS.LOOKBACK_HOURS,
            "save_raw_text": True,
            "max_single_text_chars": 40000,
            "max_pdf_fetch_per_run": LS.MAX_PDF_FETCH_PER_RUN,
            "download_pdf_for_high_value_announcements": LS.DOWNLOAD_HIGH_VALUE_PDF,
            "max_cninfo_pages_per_market": LS.MAX_CNINFO_PAGES_PER_MARKET,
            "enable_cninfo": LS.ENABLE_CNINFO,
            "enable_sse": LS.ENABLE_SSE,
            "enable_szse": LS.ENABLE_SZSE,
            "enable_tushare_news": LS.ENABLE_TUSHARE_NEWS,
            "enable_tushare_major_news": LS.ENABLE_TUSHARE_MAJOR_NEWS,
            "news_sources": LS.TUSHARE_NEWS_SOURCES,
            "major_news_sources": LS.TUSHARE_MAJOR_NEWS_SOURCES,
            "max_tushare_news_sources_per_run": LS.TUSHARE_NEWS_MAX_SOURCES_PER_RUN,
            "max_tushare_major_news_sources_per_run": LS.TUSHARE_MAJOR_NEWS_MAX_SOURCES_PER_RUN,
            "tushare_news_rate_window_seconds": LS.TUSHARE_NEWS_RATE_WINDOW_SECONDS,
            "tushare_news_rate_max_calls": LS.TUSHARE_NEWS_RATE_MAX_CALLS,
            "tushare_major_news_rate_window_seconds": LS.TUSHARE_MAJOR_NEWS_RATE_WINDOW_SECONDS,
            "tushare_major_news_rate_max_calls": LS.TUSHARE_MAJOR_NEWS_RATE_MAX_CALLS,
            "high_value_title_keywords": LS.HIGH_VALUE_TITLE_KEYWORDS,
        },
        "event_extract": {
            "enabled": True,
            "low_confidence_threshold": 0.55,
            "manual_review_threshold": 0.45,
            "max_events_per_run": LS.MAX_EVENTS_PER_RUN,
            "batch_size": LS.DEEPSEEK_BATCH_SIZE,
            "llm_min_score": LS.LLM_MIN_EVENT_SCORE,
            "max_content_chars_per_event": LS.MAX_CONTENT_CHARS_PER_EVENT,
            "save_extract_summary": LS.SAVE_EXTRACT_SUMMARY,
        },
        "data_gap_engine": {"enabled": True, "stale_hours_hard_refresh": 36, "missing_ratio_warn": 0.05, "missing_ratio_hard": 0.15, "event_trigger_recompute": True, "max_new_feature_candidates_per_day": 8},
        "research_context_pack": {"recent_event_days": 7, "max_priority_events": LS.MAX_PRIORITY_EVENTS, "include_market_state": True, "include_family_state": True, "include_data_gap_report": True},
        "market_state": {
            "enabled": bool(getattr(LS, "ENABLE_MARKET_STATE_ENGINE", True)),
            "config_path": str(getattr(LS, "MARKET_STATE_CONFIG_PATH", Path(LS.PROJECT_ROOT) / "configs" / "market_state" / "default.json")),
            "use_router_bias": bool(getattr(LS, "MARKET_STATE_USE_ROUTER_BIAS", True)),
        },
        "integrated_thesis": {
            "enabled": bool(getattr(LS, "ENABLE_INTEGRATED_THESIS", True)),
            "output_root": str(getattr(LS, "INTEGRATED_THESIS_ROOT", Path(LS.RESEARCH_ROOT) / "integrated_thesis")),
            "portfolio_budget_overlay": bool(getattr(LS, "INTEGRATED_THESIS_PORTFOLIO_BUDGET_OVERLAY", True)),
            "soft_gate_admission": bool(getattr(LS, "INTEGRATED_THESIS_SOFT_GATE_ADMISSION", True)),
        },
        "industry_router": {
            "enabled": bool(getattr(LS, "ENABLE_INDUSTRY_ROUTER", True)),
            "contract_root": str(getattr(LS, "INDUSTRY_ROUTER_CONTRACT_ROOT", Path(LS.PROJECT_ROOT) / "configs" / "industry_router")),
            "output_root": str(getattr(LS, "INDUSTRY_ROUTER_OUTPUT_ROOT", Path(LS.RESEARCH_ROOT) / "industry_router")),
            "history_lookback_days": int(getattr(LS, "INDUSTRY_ROUTER_HISTORY_LOOKBACK_DAYS", 14) or 14),
            "enable_context_pack": bool(getattr(LS, "INDUSTRY_ROUTER_ENABLE_CONTEXT_PACK", True)),
            "enable_backtest": bool(getattr(LS, "INDUSTRY_ROUTER_ENABLE_BACKTEST", True)),
            "source_fetch": {
                "enabled": bool(getattr(LS, "INDUSTRY_ROUTER_ENABLE_SOURCE_FETCH", True)),
                "timeout_seconds": int(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_TIMEOUT_SECONDS", 8) or 8),
                "cache_hours": int(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_CACHE_HOURS", 12) or 12),
                "max_sources_per_run": int(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_MAX_SOURCES_PER_RUN", 9) or 9),
                "llm_discovery_enabled": bool(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_LLM_DISCOVERY_ENABLED", True)),
                "llm_provider": str(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_LLM_PROVIDER", "deepseek_worker") or "deepseek_worker").strip(),
                "llm_timeout_seconds": int(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_LLM_TIMEOUT_SECONDS", 20) or 20),
                "llm_max_candidates": int(getattr(LS, "INDUSTRY_ROUTER_SOURCE_FETCH_LLM_MAX_CANDIDATES", 5) or 5),
            },
            "backtest": {
                "horizons": list(getattr(LS, "INDUSTRY_ROUTER_BACKTEST_HORIZONS", [1, 2]) or [1, 2]),
                "top_k": int(getattr(LS, "INDUSTRY_ROUTER_BACKTEST_TOP_K", 3) or 3),
            },
        },
        "research_brain": {"enabled": True, "planning_model": "openai_research", "worker_model": "deepseek_worker"},
        "supervisor": {"token_plan_min_interval_hours": LS.TOKEN_PLAN_MIN_INTERVAL_HOURS, "run_forever": LS.SUPERVISOR_RUN_FOREVER, "max_ticks": LS.SUPERVISOR_MAX_TICKS, "sleep_seconds": LS.SUPERVISOR_SLEEP_SECONDS, "gpu_research_max_cycles_per_tick": LS.GPU_RESEARCH_MAX_CYCLES_PER_TICK, "gpu_research_dry_run": LS.GPU_RESEARCH_DRY_RUN, "require_gpu": LS.REQUIRE_GPU},
        "dynamic_strategy": {
            "enabled": LS.ENABLE_DAILY_STRATEGY_FEEDBACK,
            "lookback_days": LS.STRATEGY_FEEDBACK_LOOKBACK_DAYS,
            "defensive_daily_return_threshold": LS.DEFENSIVE_DAILY_RETURN_THRESHOLD,
            "defensive_three_day_return_threshold": LS.DEFENSIVE_THREE_DAY_RETURN_THRESHOLD,
            "aggressive_daily_return_threshold": LS.AGGRESSIVE_DAILY_RETURN_THRESHOLD,
            "aggressive_three_day_return_threshold": LS.AGGRESSIVE_THREE_DAY_RETURN_THRESHOLD,
        },
        "research_brain": {"project_root": LS.V5_PROJECT_ROOT, "hub_output_root": LS.V5_HUB_OUTPUT_ROOT, "train_table_dir": LS.TRAIN_TABLE_DIR, "bridge_input_root": LS.V5_BRIDGE_INPUT_ROOT, "python_executable": LS.PYTHON_EXECUTABLE},
        "market_pipeline": {
            "enabled": LS.ENABLE_MARKET_PIPELINE,
            "enriched_dir": LS.ENRICHED_DAILY_DIR,
            "flags_path": LS.TRADABILITY_FLAGS_PATH,
            "hs300_path": LS.HS300_DAILY_PATH,
            "hs300_membership_history_path": LS.HS300_MEMBERSHIP_HISTORY_PATH,
            "listing_master_path": LS.LISTING_MASTER_PATH,
            "stock_universe_path": LS.STOCK_UNIVERSE_CLEAN_PATH,
            "price_snapshot_path": LS.LIVE_PRICE_SNAPSHOT_PATH,
            "realtime_quote_enabled": bool(getattr(LS, "ENABLE_TUSHARE_REALTIME_QUOTE", True)),
            "realtime_quote_source": str(getattr(LS, "TUSHARE_REALTIME_QUOTE_SOURCE", "sina") or "sina"),
            "realtime_quote_batch_size": int(getattr(LS, "TUSHARE_REALTIME_QUOTE_BATCH_SIZE", 200) or 200),
            "realtime_list_enabled": bool(getattr(LS, "ENABLE_TUSHARE_REALTIME_LIST", True)),
            "realtime_list_source": str(getattr(LS, "TUSHARE_REALTIME_LIST_SOURCE", "dc") or "dc"),
            "realtime_list_limit": int(getattr(LS, "TUSHARE_REALTIME_LIST_LIMIT", 120) or 120),
            "realtime_tick_enabled": bool(getattr(LS, "ENABLE_TUSHARE_REALTIME_TICK", True)),
            "realtime_tick_source": str(getattr(LS, "TUSHARE_REALTIME_TICK_SOURCE", "sina") or "sina"),
            "realtime_tick_symbol_limit": int(getattr(LS, "TUSHARE_REALTIME_TICK_SYMBOL_LIMIT", 6) or 6),
            "rt_min_enabled": bool(getattr(LS, "ENABLE_TUSHARE_RT_MIN", True)),
            "rt_min_provider": str(getattr(LS, "INTRADAY_RT_MIN_PROVIDER", "eastmoney") or "eastmoney").strip().lower(),
            "rt_min_fallback_provider": str(getattr(LS, "INTRADAY_RT_MIN_FALLBACK_PROVIDER", "tushare") or "tushare").strip().lower(),
            "rt_min_freq": str(getattr(LS, "TUSHARE_RT_MIN_FREQ", "1MIN") or "1MIN"),
            "rt_min_symbol_limit": int(getattr(LS, "TUSHARE_RT_MIN_SYMBOL_LIMIT", 12) or 12),
            "rapid_refresh_require_trade_session": bool(getattr(LS, "TUSHARE_RAPID_REFRESH_REQUIRE_TRADE_SESSION", True)),
            "rapid_refresh_symbol_limit": int(getattr(LS, "TUSHARE_RAPID_REFRESH_SYMBOL_LIMIT", 8) or 8),
            "rapid_refresh_skip_list": bool(getattr(LS, "TUSHARE_RAPID_REFRESH_SKIP_LIST", True)),
            "rapid_refresh_skip_tick": bool(getattr(LS, "TUSHARE_RAPID_REFRESH_SKIP_TICK", True)),
            "rapid_refresh_rt_min_enabled": bool(getattr(LS, "TUSHARE_RAPID_REFRESH_RT_MIN_ENABLED", True)),
            "intraday_proxy_root": str(getattr(LS, "INTRADAY_PROXY_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "intraday_proxy")),
            "train_append_lookback_rows": LS.TRAIN_APPEND_LOOKBACK_ROWS,
            "train_append_prefix": LS.TRAIN_APPEND_PREFIX,
            "sync_tushare_missing_days": LS.SYNC_TUSHARE_MISSING_DAYS,
        },
        "global_objective": {
            "enabled": bool(getattr(LS, "ENABLE_GLOBAL_OBJECTIVE", True)),
            "minimum_evidence_score": float(getattr(LS, "GLOBAL_OBJECTIVE_MIN_EVIDENCE_SCORE", 0.35) or 0.35),
            "maximum_harvest_risk": float(getattr(LS, "GLOBAL_OBJECTIVE_MAX_HARVEST_RISK", 0.85) or 0.85),
            "maximum_family_concentration": float(getattr(LS, "GLOBAL_OBJECTIVE_MAX_FAMILY_CONCENTRATION", 0.60) or 0.60),
            "minimum_execution_score": float(getattr(LS, "GLOBAL_OBJECTIVE_MIN_EXECUTION_SCORE", 0.30) or 0.30),
            "minimum_candidate_count": int(getattr(LS, "GLOBAL_OBJECTIVE_MIN_CANDIDATE_COUNT", 3) or 3),
            "maximum_guardrail_penalty": float(getattr(LS, "GLOBAL_OBJECTIVE_MAX_GUARDRAIL_PENALTY", 0.78) or 0.78),
            "minimum_incremental_value_score": float(getattr(LS, "GLOBAL_OBJECTIVE_MIN_INCREMENTAL_VALUE_SCORE", 0.28) or 0.28),
            "outcome_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_OUTCOME_WEIGHT", 0.22) or 0.22),
            "evidence_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_EVIDENCE_WEIGHT", 0.24) or 0.24),
            "diversity_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_DIVERSITY_WEIGHT", 0.18) or 0.18),
            "execution_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_EXECUTION_WEIGHT", 0.18) or 0.18),
            "adversarial_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_ADVERSARIAL_WEIGHT", 0.18) or 0.18),
            "guardrail_weight": float(getattr(LS, "GLOBAL_OBJECTIVE_GUARDRAIL_WEIGHT", 0.12) or 0.12),
            "exploration_budget": float(getattr(LS, "GLOBAL_OBJECTIVE_EXPLORATION_BUDGET", 0.15) or 0.15),
            "max_cycles": int(getattr(LS, "GLOBAL_OBJECTIVE_MAX_CYCLES", 3) or 3),
        },
        "affordable_data_bundle": {
            "enabled": bool(getattr(LS, "ENABLE_AFFORDABLE_DATA_BUNDLE", True)),
            "run_before_research": bool(getattr(LS, "AFFORDABLE_DATA_BUNDLE_RUN_BEFORE_RESEARCH", True)),
            "fail_open": bool(getattr(LS, "AFFORDABLE_DATA_BUNDLE_FAIL_OPEN", True)),
            "script_path": str(getattr(LS, "AFFORDABLE_DATA_BUNDLE_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "update_affordable_data_bundle.py")),
            "sqlite_path": str(getattr(LS, "AFFORDABLE_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "affordable_data_v1.sqlite3")),
            "snapshot_root": str(getattr(LS, "AFFORDABLE_SNAPSHOT_ROOT", Path(LS.DATA_ROOT) / "affordable_feeds" / "latest")),
            "daily_lookback": int(getattr(LS, "AFFORDABLE_DATA_BUNDLE_DAILY_LOOKBACK", 3) or 3),
            "announcement_lookback": int(getattr(LS, "AFFORDABLE_DATA_BUNDLE_ANNOUNCEMENT_LOOKBACK", 30) or 30),
            "timeout_minutes": int(getattr(LS, "AFFORDABLE_DATA_BUNDLE_TIMEOUT_MINUTES", 120) or 120),
            "datasets": list(
                getattr(
                    LS,
                    "AFFORDABLE_DATA_BUNDLE_DATASETS",
                    [
                        "stock_basic",
                        "daily",
                        "adj_factor",
                        "daily_basic",
                        "forecast",
                        "express",
                        "dividend",
                        "stk_holdertrade",
                        "ggt_daily",
                        "moneyflow_hsgt",
                        "hk_hold",
                        "margin",
                        "margin_detail",
                        "moneyflow",
                        "stk_limit",
                        "customs_summary",
                    ],
                )
                or []
            ),
        },
        "research_fact_refresh": {
            "enabled": bool(getattr(LS, "ENABLE_RESEARCH_FACT_REFRESH", True)),
            "run_before_research": bool(getattr(LS, "RESEARCH_FACT_REFRESH_RUN_BEFORE_RESEARCH", True)),
            "fail_open": bool(getattr(LS, "RESEARCH_FACT_REFRESH_FAIL_OPEN", True)),
            "event_script_path": str(getattr(LS, "RESEARCH_FACT_EVENT_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "build_event_fact_layer.py")),
            "hard_factor_script_path": str(getattr(LS, "RESEARCH_FACT_HARD_FACTOR_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "build_industry_hard_factor_layer.py")),
            "sqlite_path": str(getattr(LS, "RESEARCH_FACT_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_fact_layers_v1.sqlite3")),
            "event_lookback_days": int(getattr(LS, "RESEARCH_FACT_EVENT_LOOKBACK_DAYS", 60) or 60),
            "hard_factor_lookback_days": int(getattr(LS, "RESEARCH_FACT_HARD_FACTOR_LOOKBACK_DAYS", 5) or 5),
            "timeout_minutes": int(getattr(LS, "RESEARCH_FACT_REFRESH_TIMEOUT_MINUTES", 90) or 90),
        },
        "derived_alpha_refresh": {
            "enabled": bool(getattr(LS, "ENABLE_DERIVED_ALPHA_REFRESH", True)),
            "run_before_research": bool(getattr(LS, "DERIVED_ALPHA_REFRESH_RUN_BEFORE_RESEARCH", True)),
            "fail_open": bool(getattr(LS, "DERIVED_ALPHA_REFRESH_FAIL_OPEN", False)),
            "affordable_sqlite_path": str(getattr(LS, "AFFORDABLE_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "affordable_data_v1.sqlite3")),
            "runtime_sqlite_path": str(getattr(LS, "RUNTIME_DATA_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_data_v1.sqlite3")),
        },
        "external_research_refresh": {
            "enabled": bool(getattr(LS, "ENABLE_EXTERNAL_RESEARCH_REFRESH", True)),
            "run_before_research": bool(getattr(LS, "EXTERNAL_RESEARCH_REFRESH_RUN_BEFORE_RESEARCH", True)),
            "fail_open": bool(getattr(LS, "EXTERNAL_RESEARCH_REFRESH_FAIL_OPEN", True)),
            "script_path": str(getattr(LS, "EXTERNAL_RESEARCH_REFRESH_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "update_external_research_feeds.py")),
            "sqlite_path": str(getattr(LS, "RESEARCH_FACT_SQLITE_PATH", Path(LS.DATA_ROOT) / "sql_store" / "research_fact_layers_v1.sqlite3")),
            "artifact_root": str(getattr(LS, "EXTERNAL_RESEARCH_ROOT", Path(LS.DATA_ROOT) / "external_research_feeds" / "latest")),
            "seed_path": str(getattr(LS, "EXTERNAL_RESEARCH_SEED_PATH", Path(LS.REPO_ROOT) / "configs" / "external_sources" / "qianzhan_seed_urls.json")),
            "fetch_timeout_seconds": int(getattr(LS, "EXTERNAL_RESEARCH_FETCH_TIMEOUT_SECONDS", 20) or 20),
            "qianzhan_enabled": bool(getattr(LS, "EXTERNAL_RESEARCH_QIANZHAN_ENABLED", True)),
            "qianzhan_cookie_header_path": str(getattr(LS, "EXTERNAL_RESEARCH_QIANZHAN_COOKIE_HEADER_PATH", Path(LS.DATA_ROOT) / "private" / "qianzhan_cookie_header.txt")),
            "qianzhan_daily_page_budget": int(getattr(LS, "EXTERNAL_RESEARCH_QIANZHAN_DAILY_PAGE_BUDGET", 24) or 24),
            "ggzy_enabled": bool(getattr(LS, "EXTERNAL_RESEARCH_GGZY_ENABLED", True)),
            "ggzy_max_notices_per_run": int(getattr(LS, "EXTERNAL_RESEARCH_GGZY_MAX_NOTICES_PER_RUN", 36) or 36),
            "llm_enrichment_enabled": bool(getattr(LS, "EXTERNAL_RESEARCH_LLM_ENRICHMENT_ENABLED", True)),
            "llm_provider": str(getattr(LS, "EXTERNAL_RESEARCH_LLM_PROVIDER", "deepseek_worker") or "deepseek_worker"),
            "llm_timeout_seconds": int(getattr(LS, "EXTERNAL_RESEARCH_LLM_TIMEOUT_SECONDS", 30) or 30),
        },
        "t_audit": {
            "enabled": bool(getattr(LS, "ENABLE_T_AUDIT", True)),
            "artifact_root": str(getattr(LS, "T_AUDIT_OUTPUT_ROOT", Path(LS.DATA_ROOT) / "audit_v1")),
            "policy_path": str(getattr(LS, "T_AUDIT_POLICY_PATH", Path(LS.PROJECT_ROOT) / "configs" / "t_overlay" / "t_audit_policy.json")),
        },
        "audit_site_publish": {
            "enabled": bool(getattr(LS, "ENABLE_AUDIT_SITE_PUBLISH", True)),
            "run_after_summary": bool(getattr(LS, "AUDIT_SITE_PUBLISH_RUN_AFTER_SUMMARY", True)),
            "fail_open": bool(getattr(LS, "AUDIT_SITE_PUBLISH_FAIL_OPEN", True)),
            "script_path": str(getattr(LS, "AUDIT_SITE_PUBLISH_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "publish_audit_report_to_site.ps1")),
            "powershell_executable": str(getattr(LS, "AUDIT_SITE_PUBLISH_POWERSHELL", "powershell.exe") or "powershell.exe"),
            "python_executable": str(getattr(LS, "PYTHON_EXECUTABLE", sys.executable) or sys.executable),
            "remote_user": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_USER", "ubuntu") or "ubuntu"),
            "remote_host": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_HOST", "43.129.28.141") or "43.129.28.141"),
            "remote_root": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_ROOT", "/var/www/peng1145141919810.xyz/site") or "/var/www/peng1145141919810.xyz/site"),
            "domain": str(getattr(LS, "AUDIT_SITE_PUBLISH_DOMAIN", "peng1145141919810.xyz") or "peng1145141919810.xyz"),
            "timeout_minutes": int(getattr(LS, "AUDIT_SITE_PUBLISH_TIMEOUT_MINUTES", 20) or 20),
        },
        "operator_runtime_publish": {
            "enabled": bool(getattr(LS, "ENABLE_OPERATOR_RUNTIME_PUBLISH", True)),
            "fail_open": bool(getattr(LS, "OPERATOR_RUNTIME_PUBLISH_FAIL_OPEN", True)),
            "script_path": str(getattr(LS, "OPERATOR_RUNTIME_PUBLISH_SCRIPT_PATH", Path(LS.PROJECT_ROOT).parents[1] / "scripts" / "publish_operator_runtime_context_to_site.ps1")),
            "powershell_executable": str(getattr(LS, "OPERATOR_RUNTIME_PUBLISH_POWERSHELL", "powershell.exe") or "powershell.exe"),
            "python_executable": str(getattr(LS, "PYTHON_EXECUTABLE", sys.executable) or sys.executable),
            "remote_user": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_USER", "ubuntu") or "ubuntu"),
            "remote_host": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_HOST", "43.129.28.141") or "43.129.28.141"),
            "remote_root": str(getattr(LS, "AUDIT_SITE_PUBLISH_REMOTE_ROOT", "/var/www/peng1145141919810.xyz/site") or "/var/www/peng1145141919810.xyz/site"),
            "timeout_minutes": int(getattr(LS, "OPERATOR_RUNTIME_PUBLISH_TIMEOUT_MINUTES", 5) or 5),
            "min_interval_seconds": int(getattr(LS, "OPERATOR_RUNTIME_PUBLISH_MIN_INTERVAL_SECONDS", 300) or 300),
        },
        "intraday_state_machine": {
            "enabled": bool(getattr(LS, "ENABLE_INTRADAY_STATE_MACHINE", True)),
            "shadow_mode": bool(getattr(LS, "INTRADAY_STATE_MACHINE_SHADOW_MODE", True)),
            "afternoon_overlay_respect_shadow_mode": bool(getattr(LS, "INTRADAY_AFTERNOON_OVERLAY_RESPECT_SHADOW_MODE", False)),
            "fail_open": bool(getattr(LS, "INTRADAY_STATE_MACHINE_FAIL_OPEN", True)),
            "enable_afternoon_overlay": bool(getattr(LS, "INTRADAY_STATE_MACHINE_ENABLE_AFTERNOON_OVERLAY", True)),
            "stale_order_minutes": int(getattr(LS, "INTRADAY_STATE_MACHINE_STALE_ORDER_MINUTES", 20) or 20),
            "strict_pre_execution_gate": bool(getattr(LS, "INTRADAY_STATE_MACHINE_STRICT_PRE_EXECUTION_GATE", False)),
            "artifact_root": str(getattr(LS, "INTRADAY_STATE_MACHINE_ROOT", Path(LS.DATA_ROOT) / "trade_clock" / "intraday_state")),
            "refresh_on_phase_completion": list(
                getattr(
                    LS,
                    "INTRADAY_STATE_MACHINE_REFRESH_PHASES",
                    ["preopen_gate", "simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow", "summary"],
                )
                or ["preopen_gate", "simulation", "shadow", "midday_review", "afternoon_execution", "afternoon_shadow", "summary"]
            ),
            "timing_layer": {
                "enabled": bool(getattr(LS, "ENABLE_EXECUTION_TIMING_LAYER", True)),
                "window_config": dict(
                    getattr(
                        LS,
                        "TIMING_LAYER_WINDOW_CONFIG",
                        {
                            "open_noise_window": {"start": "09:30:00", "end": "09:40:00", "allow_trim": True, "allow_exit": True},
                            "morning_primary_window": {
                                "start": "09:40:00",
                                "end": "10:30:00",
                                "allow_new_entry": True,
                                "allow_build_entry": True,
                                "allow_trim": True,
                                "allow_exit": True,
                                "allow_t_first_leg": True,
                            },
                            "mid_morning_low_speed_window": {
                                "start": "10:30:00",
                                "end": "11:20:00",
                                "allow_trim": True,
                                "allow_exit": True,
                                "allow_reconcile": True,
                            },
                            "afternoon_primary_window": {
                                "start": "13:00:00",
                                "end": "14:20:00",
                                "allow_new_entry": True,
                                "allow_build_entry": True,
                                "allow_trim": True,
                                "allow_exit": True,
                                "allow_t_second_leg": True,
                            },
                            "late_afternoon_reconcile_window": {
                                "start": "14:20:00",
                                "end": "14:50:00",
                                "allow_trim": True,
                                "allow_exit": True,
                                "allow_reconcile": True,
                                "allow_t_second_leg": True,
                            },
                            "post_1450_close_only_window": {
                                "start": "14:50:00",
                                "end": "15:00:00",
                                "allow_exit": True,
                                "allow_reconcile": True,
                            },
                        },
                    )
                    or {}
                ),
                "buy_score_threshold": float(getattr(LS, "TIMING_LAYER_BUY_SCORE_THRESHOLD", 0.58) or 0.58),
                "sell_score_threshold": float(getattr(LS, "TIMING_LAYER_SELL_SCORE_THRESHOLD", 0.62) or 0.62),
                "require_oms_clean_state": bool(getattr(LS, "TIMING_LAYER_REQUIRE_OMS_CLEAN_STATE", False)),
                "require_flow_confirmation": bool(getattr(LS, "TIMING_LAYER_REQUIRE_FLOW_CONFIRMATION", False)),
                "enable_afternoon_second_leg": bool(getattr(LS, "TIMING_LAYER_ENABLE_AFTERNOON_SECOND_LEG", True)),
            },
            "t_overlay": {
                "enabled": bool(getattr(LS, "ENABLE_T_OVERLAY", True)),
                "max_rounds_per_symbol_per_day": int(getattr(LS, "T_OVERLAY_MAX_ROUNDS_PER_SYMBOL_PER_DAY", 1) or 1),
                "max_ratio_per_symbol": float(getattr(LS, "T_OVERLAY_MAX_RATIO_PER_SYMBOL", 0.20) or 0.20),
                "disable_on_panic": bool(getattr(LS, "T_OVERLAY_DISABLE_ON_PANIC", False)),
                "disable_on_major_event": bool(getattr(LS, "T_OVERLAY_DISABLE_ON_MAJOR_EVENT", True)),
                "concentration_guard": {
                    "high_risk_max_proxy_spread": float(getattr(LS, "T_OVERLAY_HIGH_RISK_MAX_PROXY_SPREAD", 0.015) or 0.015),
                    "elevated_max_proxy_spread": float(getattr(LS, "T_OVERLAY_ELEVATED_MAX_PROXY_SPREAD", 0.018) or 0.018),
                    "high_risk_max_ratio_multiplier": float(getattr(LS, "T_OVERLAY_HIGH_RISK_MAX_RATIO_MULTIPLIER", 0.65) or 0.65),
                    "elevated_max_ratio_multiplier": float(getattr(LS, "T_OVERLAY_ELEVATED_MAX_RATIO_MULTIPLIER", 0.82) or 0.82),
                },
            },
        },
        "intraday_tactics": {
            "enabled": bool(getattr(LS, "ENABLE_INTRADAY_TACTICS", True)),
            "artifact_root": str(getattr(LS, "INTRADAY_TACTICS_ARTIFACT_ROOT", Path(LS.DATA_ROOT) / "trade_clock" / "intraday_tactics")),
            "max_daily_turnover_ratio": float(getattr(LS, "INTRADAY_TACTICAL_MAX_DAILY_TURNOVER_RATIO", 0.12) or 0.12),
            "max_symbol_add_ratio": float(getattr(LS, "INTRADAY_TACTICAL_MAX_SYMBOL_ADD_RATIO", 0.06) or 0.06),
            "max_symbol_reduce_ratio": float(getattr(LS, "INTRADAY_TACTICAL_MAX_SYMBOL_REDUCE_RATIO", 0.25) or 0.25),
            "buy_cooldown_minutes": int(getattr(LS, "INTRADAY_TACTICAL_BUY_COOLDOWN_MINUTES", 18) or 18),
            "sell_cooldown_minutes": int(getattr(LS, "INTRADAY_TACTICAL_SELL_COOLDOWN_MINUTES", 5) or 5),
            "allow_add_on_snapshot_degraded": bool(getattr(LS, "INTRADAY_TACTICAL_ALLOW_ADD_ON_SNAPSHOT_DEGRADED", True)),
            "allow_reduce_on_snapshot_degraded": bool(getattr(LS, "INTRADAY_TACTICAL_ALLOW_REDUCE_ON_SNAPSHOT_DEGRADED", True)),
            "enable_time_stop": bool(getattr(LS, "INTRADAY_TACTICAL_ENABLE_TIME_STOP", True)),
            "enable_take_profit": bool(getattr(LS, "INTRADAY_TACTICAL_ENABLE_TAKE_PROFIT", True)),
            "enable_stop_loss": bool(getattr(LS, "INTRADAY_TACTICAL_ENABLE_STOP_LOSS", True)),
            "enable_t_overlay": bool(getattr(LS, "INTRADAY_TACTICAL_ENABLE_T_OVERLAY", True)),
            "enable_tactical_add": bool(getattr(LS, "INTRADAY_TACTICAL_ENABLE_TACTICAL_ADD", True)),
            "reason_thresholds": dict(
                getattr(
                    LS,
                    "INTRADAY_TACTICAL_REASON_THRESHOLDS",
                    {
                        "take_profit_soft_pct": 0.035,
                        "take_profit_hard_pct": 0.08,
                        "stop_loss_soft_pct": 0.025,
                        "stop_loss_hard_pct": 0.06,
                        "time_stop_minutes": 120,
                    },
                )
                or {}
            ),
            "scheduler_phases": dict(
                getattr(
                    LS,
                    "INTRADAY_TACTICS_SCHEDULER_PHASES",
                    {
                        "intraday_tactical_0940": {"enabled": True, "time": "09:40:00", "timeout_minutes": 8},
                        "intraday_tactical_1010": {"enabled": True, "time": "10:10:00", "timeout_minutes": 8},
                        "intraday_tactical_1040": {"enabled": True, "time": "10:40:00", "timeout_minutes": 8},
                        "intraday_tactical_1310": {"enabled": True, "time": "13:10:00", "timeout_minutes": 8},
                        "intraday_tactical_1350": {"enabled": True, "time": "13:50:00", "timeout_minutes": 8},
                        "intraday_tactical_1420": {"enabled": True, "time": "14:20:00", "timeout_minutes": 8},
                    },
                )
                or {}
            ),
        },
        "portfolio_recommendation": {
            "enabled": LS.ENABLE_PORTFOLIO_RECOMMENDATION,
            "max_names": LS.PORTFOLIO_MAX_NAMES,
            "single_name_cap": LS.PORTFOLIO_SINGLE_NAME_CAP,
            "total_exposure_cap": LS.PORTFOLIO_TOTAL_EXPOSURE_CAP,
            "simulation_ready_need_gate": LS.PORTFOLIO_SIMULATION_READY_NEED_GATE,
            "market_state_aware_sizing": bool(getattr(LS, "PORTFOLIO_MARKET_STATE_AWARE_SIZING", True)),
            "technical_confirmation_gate": bool(getattr(LS, "PORTFOLIO_TECHNICAL_CONFIRMATION_GATE", False)),
            "enable_post_filter_reweight": bool(getattr(LS, "PORTFOLIO_ENABLE_POST_FILTER_REWEIGHT", True)),
            "min_exposure_fill_ratio": float(getattr(LS, "PORTFOLIO_MIN_EXPOSURE_FILL_RATIO", 0.75) or 0.75),
            "enforce_executable_universe": bool(getattr(LS, "PORTFOLIO_ENFORCE_EXECUTABLE_UNIVERSE", True)),
            "executable_allowed_suffixes": list(getattr(LS, "PORTFOLIO_EXECUTABLE_ALLOWED_SUFFIXES", [".SH", ".SZ"]) or [".SH", ".SZ"]),
            "require_tradable_basic": bool(getattr(LS, "PORTFOLIO_EXECUTABLE_REQUIRE_TRADABLE_BASIC", True)),
            "account_size_aware_sizing": bool(getattr(LS, "PORTFOLIO_ACCOUNT_SIZE_AWARE_SIZING", True)),
            "account_size_slot_budget_ratio": float(getattr(LS, "PORTFOLIO_ACCOUNT_SIZE_SLOT_BUDGET_RATIO", 0.96) or 0.96),
            "account_size_min_weight_buffer": float(getattr(LS, "PORTFOLIO_ACCOUNT_SIZE_MIN_WEIGHT_BUFFER", 1.05) or 1.05),
            "account_size_max_single_name_cap": float(getattr(LS, "PORTFOLIO_ACCOUNT_SIZE_MAX_SINGLE_NAME_CAP", 0.35) or 0.35),
            "broad_candidate_pool_limit": int(getattr(LS, "PORTFOLIO_BROAD_CANDIDATE_POOL_LIMIT", 48) or 48),
            "hard_data_candidate_pool_enabled": bool(getattr(LS, "PORTFOLIO_HARD_DATA_CANDIDATE_POOL_ENABLED", True)),
            "hard_data_candidate_weights": dict(
                getattr(
                    LS,
                    "PORTFOLIO_HARD_DATA_CANDIDATE_WEIGHTS",
                    {"seed_weight": 0.18, "pred_score": 0.42, "valuation": 0.24, "liquidity": 0.16},
                )
                or {}
            ),
            "llm_candidate_weak_accept_threshold": int(getattr(LS, "PORTFOLIO_LLM_CANDIDATE_WEAK_ACCEPT_THRESHOLD", 6) or 6),
            "enable_intelligent_outer_allocator": bool(getattr(LS, "PORTFOLIO_ENABLE_INTELLIGENT_OUTER_ALLOCATOR", False)),
            "intelligent_outer_allocator_replaces_internal_gates": bool(getattr(LS, "PORTFOLIO_INTELLIGENT_OUTER_ALLOCATOR_REPLACES_INTERNAL_GATES", False)),
            "pre_release_intraday_proxy_objective_enabled": bool(getattr(LS, "PORTFOLIO_PRE_RELEASE_INTRADAY_PROXY_OBJECTIVE_ENABLED", True)),
            "pre_release_proxy_max_spread_pct": float(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_MAX_SPREAD_PCT", 0.028) or 0.028),
            "pre_release_proxy_turnover_enforce": bool(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_TURNOVER_ENFORCE", True)),
            "pre_release_proxy_diversification_flatten": bool(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_DIVERSIFICATION_FLATTEN", True)),
            "pre_release_proxy_cash_headroom_floor": float(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_CASH_HEADROOM_FLOOR", 0.04) or 0.04),
            "pre_release_proxy_hhi_trigger": float(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_HHI_TRIGGER", 0.18) or 0.18),
            "pre_release_proxy_flatten_gamma": float(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_FLATTEN_GAMMA", 0.94) or 0.94),
            "pre_release_proxy_fail_open": bool(getattr(LS, "PORTFOLIO_PRE_RELEASE_PROXY_FAIL_OPEN", True)),
        },
        "portfolio_candidate_llm_review": {
            "enabled": bool(getattr(LS, "ENABLE_PORTFOLIO_CANDIDATE_LLM_REVIEW", False)),
            "provider": str(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_PROVIDER", "deepseek_worker") or "deepseek_worker"),
            "timeout_seconds": int(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_TIMEOUT_SECONDS", 45) or 45),
            "max_input_rows": int(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_MAX_INPUT_ROWS", 18) or 18),
            "artifact_root": str(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_ARTIFACT_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "candidate_pool_llm_review")),
            "llm_symbol_boost": float(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_SYMBOL_BOOST", 0.10) or 0.10),
            "llm_mechanism_boost": float(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_MECHANISM_BOOST", 0.05) or 0.05),
            "llm_event_boost": float(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_EVENT_BOOST", 0.04) or 0.04),
            "llm_blocked_penalty": float(getattr(LS, "PORTFOLIO_CANDIDATE_LLM_BLOCKED_PENALTY", 0.25) or 0.25),
        },
        "strategy_activation": {
            "enabled": bool(getattr(LS, "ENABLE_STRATEGY_ACTIVATION", True)),
            "blend_weight": float(getattr(LS, "STRATEGY_ACTIVATION_BLEND_WEIGHT", 0.32) or 0.32),
            "llm_enabled": bool(getattr(LS, "ENABLE_STRATEGY_ACTIVATION_LLM", False)),
            "llm_provider": str(getattr(LS, "STRATEGY_ACTIVATION_LLM_PROVIDER", "deepseek_worker") or "deepseek_worker").strip(),
            "llm_timeout_seconds": int(getattr(LS, "STRATEGY_ACTIVATION_LLM_TIMEOUT_SECONDS", 40) or 40),
            "llm_max_input_rows": int(getattr(LS, "STRATEGY_ACTIVATION_LLM_MAX_INPUT_ROWS", 16) or 16),
            "artifact_root": str(getattr(LS, "STRATEGY_ACTIVATION_LLM_ARTIFACT_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "strategy_activation_llm")),
            "signal_weights": {
                "valuation": {
                    "pe_pct_industry": 0.35,
                    "pb_pct_industry": 0.35,
                    "ps_pct_industry": 0.30,
                },
                "revision": {
                    "revision_score": 0.60,
                    "eps_revision_7d": 0.20,
                    "eps_revision_30d": 0.20,
                },
                "order_flow": {
                    "recent_contract_count": 0.18,
                    "recent_contract_amount": 0.22,
                    "backlog_amount": 0.24,
                    "contract_liability": 0.14,
                    "has_major_contract": 0.12,
                    "has_bid_award": 0.10,
                },
                "event_drive": {
                    "fact_contract_count": 0.25,
                    "fact_contract_amount": 0.25,
                    "fact_government_contracts": 0.10,
                    "positive_supply_signals": 0.25,
                    "total_supply_signals": 0.05,
                    "negative_supply_signals_inverted": 0.10,
                },
                "industry": {
                    "qianzhan_relevance": 0.45,
                    "qianzhan_direction_bias": 0.20,
                    "inventory_direction_bias": 0.20,
                    "operation_direction_bias": 0.15,
                },
                "liquidity": {
                    "turnover_pct_rank": 0.34,
                    "crowding_score_inverted": 0.22,
                    "fund_exposure_proxy": 0.18,
                    "northbound_holding_change": 0.13,
                    "margin_balance_change": 0.13,
                },
            },
            "meta_weights": {
                "valuation_signal_score": 0.56,
                "revision_signal_score": 0.08,
                "order_flow_signal_score": 0.00,
                "event_drive_signal_score": 0.00,
                "industry_signal_score": 0.00,
                "liquidity_signal_score": 0.36,
            },
            "priority_weights": {
                "selection_score_base": 0.66,
                "selection_score_risk_budget_bonus": 0.12,
                "data_activation_score": 0.34,
            },
            "llm_overlay": {
                "favored_symbol_boost": 0.09,
                "favored_family_boost": 0.05,
                "blocked_symbol_penalty": 0.16,
                "aggressiveness_min": 0.85,
                "aggressiveness_max": 1.35,
            },
            "risk_budget_bounds": {
                "min": 0.35,
                "max": 1.6,
                "priority_bonus_cap": 1.0,
            },
            "cash_multipliers": {
                "high_cash_threshold": 0.15,
                "mid_cash_threshold": 0.08,
                "high_cash_multiplier": 1.0,
                "mid_cash_multiplier": 0.94,
                "low_cash_multiplier": 0.88,
            },
        },
        "evidence_audit": {
            "enabled": bool(getattr(LS, "ENABLE_EVIDENCE_AUDIT", True)),
            "portfolio_gate_enabled": bool(getattr(LS, "EVIDENCE_AUDIT_PORTFOLIO_GATE_ENABLED", True)),
            "run_after_portfolio_recommendation": bool(getattr(LS, "EVIDENCE_AUDIT_RUN_AFTER_PORTFOLIO_RECOMMENDATION", True)),
            "rebuild_portfolio_after_audit": bool(getattr(LS, "EVIDENCE_AUDIT_REBUILD_PORTFOLIO_AFTER_AUDIT", True)),
            "block_execution_on_failure": bool(getattr(LS, "EVIDENCE_AUDIT_BLOCK_EXECUTION_ON_FAILURE", True)),
            "llm_provider": str(getattr(LS, "EVIDENCE_AUDIT_LLM_PROVIDER", "deepseek_worker") or "deepseek_worker").strip(),
            "llm_timeout_seconds": int(getattr(LS, "EVIDENCE_AUDIT_LLM_TIMEOUT_SECONDS", 60) or 60),
            "max_candidates": int(getattr(LS, "EVIDENCE_AUDIT_MAX_CANDIDATES", 40) or 40),
            "max_results_per_query": int(getattr(LS, "EVIDENCE_AUDIT_MAX_RESULTS_PER_QUERY", 3) or 3),
            "max_sources_per_symbol": int(getattr(LS, "EVIDENCE_AUDIT_MAX_SOURCES_PER_SYMBOL", 8) or 8),
            "fetch_timeout_seconds": int(getattr(LS, "EVIDENCE_AUDIT_FETCH_TIMEOUT_SECONDS", 12) or 12),
            "max_page_chars": int(getattr(LS, "EVIDENCE_AUDIT_MAX_PAGE_CHARS", 8000) or 8000),
            "artifact_root": str(getattr(LS, "EVIDENCE_AUDIT_ARTIFACT_ROOT", Path(LS.PORTFOLIO_OUTPUT_ROOT) / "evidence_audit_v1")),
            "allow_domains": list(
                getattr(
                    LS,
                    "EVIDENCE_AUDIT_ALLOW_DOMAINS",
                    ["cninfo.com.cn", "sse.com.cn", "szse.cn", "eastmoney.com", "stcn.com", "cs.com.cn", "thepaper.cn", "gov.cn", "ccgp.gov.cn"],
                )
                or []
            ),
            "grade_weight_multipliers": dict(
                getattr(
                    LS,
                    "EVIDENCE_AUDIT_GRADE_WEIGHT_MULTIPLIERS",
                    {"A": 1.10, "B": 1.04, "C": 0.92, "D": 0.55, "F": 0.0},
                )
                or {}
            ),
            "query_templates": list(
                getattr(
                    LS,
                    "EVIDENCE_AUDIT_QUERY_TEMPLATES",
                    [
                        "{base} {code} 公告 业绩预告 业绩快报 利润增长",
                        "{base} {code} 年报 季报 营收 净利润 现金流 毛利率",
                        "{base} {code} 中标 合同 订单 客户 供货",
                        "{base} {code} 减持 质押 冻结 立案 调查 处罚 诉讼",
                        "{base} {code} 退市风险 ST 非标审计 业绩亏损",
                        "{base} {code} 涨价 产能 扩产 停产 产品价格",
                    ],
                )
                or []
            ),
        },
        "portfolio": {
            "enabled": bool(getattr(LS, "ENABLE_PORTFOLIO_V2A", True)),
            "lifecycle_state_machine_enabled": bool(getattr(LS, "PORTFOLIO_ENABLE_LIFECYCLE_STATE_MACHINE", True)),
            "admission_replacement_enabled": bool(getattr(LS, "PORTFOLIO_ENABLE_ADMISSION_REPLACEMENT", True)),
            "soft_crowding_penalty_enabled": bool(getattr(LS, "PORTFOLIO_ENABLE_SOFT_CROWDING_PENALTY", True)),
            "rich_audit_enabled": bool(getattr(LS, "PORTFOLIO_ENABLE_RICH_PORTFOLIO_AUDIT", True)),
            "output_root": str(Path(LS.PORTFOLIO_OUTPUT_ROOT) / "portfolio"),
            "pilot_max_weight": float(getattr(LS, "PORTFOLIO_V2A_PILOT_MAX_WEIGHT", 0.04) or 0.04),
            "build_speed": float(getattr(LS, "PORTFOLIO_V2A_BUILD_SPEED", 1.25) or 1.25),
            "trim_speed": float(getattr(LS, "PORTFOLIO_V2A_TRIM_SPEED", 0.72) or 0.72),
            "replacement_improvement_threshold": float(getattr(LS, "PORTFOLIO_V2A_REPLACEMENT_IMPROVEMENT_THRESHOLD", 0.08) or 0.08),
            "soft_crowding_penalty_strength": float(getattr(LS, "PORTFOLIO_V2A_SOFT_CROWDING_PENALTY_STRENGTH", 0.08) or 0.08),
        },
        "technical_confirmation": {
            "enabled": bool(getattr(LS, "ENABLE_TECHNICAL_CONFIRMATION", True)),
            "config_path": str(getattr(LS, "TECHNICAL_CONFIRMATION_CONFIG_PATH", Path(LS.PROJECT_ROOT) / "configs" / "technical_confirmation" / "default.json")),
        },
        "portfolio_control": {
            "enabled": bool(getattr(LS, "ENABLE_PORTFOLIO_CONTROL", True)),
            "drift_threshold": float(getattr(LS, "PORTFOLIO_CONTROL_DRIFT_THRESHOLD", 0.005) or 0.005),
            "max_daily_turnover_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_MAX_DAILY_TURNOVER_RATIO", 0.25) or 0.25),
            "dynamic_account_scaling_enabled": bool(getattr(LS, "PORTFOLIO_CONTROL_DYNAMIC_ACCOUNT_SCALING_ENABLED", True)),
            "dynamic_account_max_turnover_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_DYNAMIC_ACCOUNT_MAX_TURNOVER_RATIO", 0.9) or 0.9),
            "dynamic_account_max_single_buy_nav_ratio_cap": float(getattr(LS, "PORTFOLIO_CONTROL_DYNAMIC_ACCOUNT_MAX_SINGLE_BUY_NAV_RATIO_CAP", 0.45) or 0.45),
            "dynamic_account_max_single_buy_cash_ratio_cap": float(getattr(LS, "PORTFOLIO_CONTROL_DYNAMIC_ACCOUNT_MAX_SINGLE_BUY_CASH_RATIO_CAP", 0.5) or 0.5),
            "enable_execution_feedback": bool(getattr(LS, "PORTFOLIO_CONTROL_ENABLE_EXECUTION_FEEDBACK", True)),
            "enable_dev_log_snapshot": bool(getattr(LS, "PORTFOLIO_CONTROL_ENABLE_DEV_LOG_SNAPSHOT", True)),
            "dev_log_top_holdings": int(getattr(LS, "PORTFOLIO_CONTROL_DEV_LOG_TOP_HOLDINGS", 8) or 8),
            "allow_odd_lot_exit": bool(getattr(LS, "PORTFOLIO_CONTROL_ALLOW_ODD_LOT_EXIT", True)),
            "bootstrap_diversification_enabled": bool(getattr(LS, "PORTFOLIO_CONTROL_BOOTSTRAP_DIVERSIFICATION_ENABLED", True)),
            "bootstrap_max_current_exposure_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_BOOTSTRAP_MAX_CURRENT_EXPOSURE_RATIO", 0.05) or 0.05),
            "bootstrap_min_names": int(getattr(LS, "PORTFOLIO_CONTROL_BOOTSTRAP_MIN_NAMES", 5) or 5),
            "bootstrap_slot_budget_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_BOOTSTRAP_SLOT_BUDGET_RATIO", 0.9) or 0.9),
            "small_account_slicing_enabled": bool(getattr(LS, "PORTFOLIO_CONTROL_SMALL_ACCOUNT_SLICING_ENABLED", True)),
            "small_account_nav_threshold": float(getattr(LS, "PORTFOLIO_CONTROL_SMALL_ACCOUNT_NAV_THRESHOLD", 50000.0) or 50000.0),
            "small_account_max_single_buy_nav_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_SMALL_ACCOUNT_MAX_SINGLE_BUY_NAV_RATIO", 0.22) or 0.22),
            "small_account_max_single_buy_cash_ratio": float(getattr(LS, "PORTFOLIO_CONTROL_SMALL_ACCOUNT_MAX_SINGLE_BUY_CASH_RATIO", 0.28) or 0.28),
            "llm_blocked_symbols": [],
            "llm_favored_symbols": [],
            "llm_favored_score_boost": float(getattr(LS, "PORTFOLIO_CONTROL_LLM_FAVORED_SCORE_BOOST", 75.0) or 75.0),
            "reduce_only": False,
        },
        "oms": {
            "enabled": bool(getattr(LS, "ENABLE_OMS", True)),
            "output_root": str(getattr(LS, "OMS_OUTPUT_ROOT", Path(LS.LIVE_EXECUTION_ROOT) / "oms_v1")),
            "use_broker_truth_for_v2a_continuity": bool(getattr(LS, "OMS_USE_BROKER_TRUTH_FOR_V2A_CONTINUITY", True)),
            "intent_expiry_days": int(getattr(LS, "OMS_INTENT_EXPIRY_DAYS", 3) or 3),
            "control_feedback_lookback_runs": int(getattr(LS, "OMS_CONTROL_FEEDBACK_LOOKBACK_RUNS", 20) or 20),
            "research_meta_lookback_runs": int(getattr(LS, "OMS_RESEARCH_META_LOOKBACK_RUNS", 60) or 60),
            "compat_write_latest_account_state": bool(getattr(LS, "OMS_COMPAT_WRITE_LATEST_ACCOUNT_STATE", True)),
            "enable_broker_cancel": bool(getattr(LS, "OMS_ENABLE_BROKER_CANCEL", True)),
        },
        "safety": {
            "enabled": bool(getattr(LS, "ENABLE_SAFETY_LAYER", True)),
            "health_probe_interval_seconds": int(getattr(LS, "SAFETY_HEALTH_PROBE_INTERVAL_SECONDS", 300) or 300),
            "account_state_max_age_seconds": int(getattr(LS, "SAFETY_ACCOUNT_STATE_MAX_AGE_SECONDS", 900) or 900),
            "position_sync_max_age_seconds": int(getattr(LS, "SAFETY_POSITION_SYNC_MAX_AGE_SECONDS", 900) or 900),
            "release_max_age_seconds": int(getattr(LS, "SAFETY_RELEASE_MAX_AGE_SECONDS", 172800) or 172800),
            "fail_on_unfinished_orders": bool(getattr(LS, "SAFETY_FAIL_ON_UNFINISHED_ORDERS", True)),
            "fail_on_unknown_order_status": bool(getattr(LS, "SAFETY_FAIL_ON_UNKNOWN_ORDER_STATUS", True)),
            "degraded_reduce_only": bool(getattr(LS, "SAFETY_DEGRADED_REDUCE_ONLY", True)),
            "caution_turnover_multiplier": float(getattr(LS, "SAFETY_CAUTION_TURNOVER_MULTIPLIER", 0.5) or 0.5),
            "market_caution_mean_pct_chg": float(getattr(LS, "SAFETY_CAUTION_MARKET_MEAN_PCT_CHG", -1.0) or -1.0),
            "market_panic_mean_pct_chg": float(getattr(LS, "SAFETY_PANIC_MARKET_MEAN_PCT_CHG", -2.2) or -2.2),
            "market_caution_hs300_return_pct": float(getattr(LS, "SAFETY_CAUTION_HS300_RETURN_PCT", -1.5) or -1.5),
            "market_panic_hs300_return_pct": float(getattr(LS, "SAFETY_PANIC_HS300_RETURN_PCT", -3.0) or -3.0),
            "market_caution_limit_down_ratio": float(getattr(LS, "SAFETY_CAUTION_LIMIT_DOWN_RATIO", 0.05) or 0.05),
            "market_panic_limit_down_ratio": float(getattr(LS, "SAFETY_PANIC_LIMIT_DOWN_RATIO", 0.12) or 0.12),
            "execution_fail_ratio_degraded": float(getattr(LS, "SAFETY_EXECUTION_FAIL_RATIO_DEGRADED", 0.35) or 0.35),
            "execution_fail_ratio_halt": float(getattr(LS, "SAFETY_EXECUTION_FAIL_RATIO_HALT", 0.75) or 0.75),
            "execution_fail_min_orders": int(getattr(LS, "SAFETY_EXECUTION_FAIL_MIN_ORDERS", 3) or 3),
        },
        "execution_policy": {
            "account_mode": str(getattr(LS, "EXECUTION_ACCOUNT_MODE", "simulation") or "simulation").strip().lower(),
            "precision_trade_enabled": bool(getattr(LS, "PRECISION_TRADE_ENABLED", True)),
            "allow_integrated_precision_execution": bool(getattr(LS, "ALLOW_INTEGRATED_PRECISION_EXECUTION", True)),
            "ignore_market_panic_reduce_only": bool(getattr(LS, "EXECUTION_IGNORE_MARKET_PANIC_REDUCE_ONLY", False)),
            "allow_unfinished_orders_reconcile": bool(getattr(LS, "EXECUTION_ALLOW_UNFINISHED_ORDERS_RECONCILE", False)),
            "namespace": "main",
            "shadow_run": False,
        },
        "execution_management": {
            "enabled": bool(getattr(LS, "ENABLE_EXECUTION_MANAGEMENT", True)),
            "max_child_order_ratio": float(getattr(LS, "EMS_MAX_CHILD_ORDER_RATIO", 0.20) or 0.20),
            "staged_entry_delay_seconds": int(getattr(LS, "EMS_STAGED_ENTRY_DELAY_SECONDS", 45) or 45),
            "allow_cancel_replace": bool(getattr(LS, "EMS_ALLOW_CANCEL_REPLACE", True)),
        },
        "execution_llm_review": {
            "enabled": bool(getattr(LS, "ENABLE_EXECUTION_LLM_REVIEW", True)),
            "provider": str(getattr(LS, "EXECUTION_LLM_REVIEW_PROVIDER", "deepseek_worker") or "deepseek_worker").strip(),
            "timeout_seconds": int(getattr(LS, "EXECUTION_LLM_REVIEW_TIMEOUT_SECONDS", 45) or 45),
            "max_target_items": int(getattr(LS, "EXECUTION_LLM_REVIEW_MAX_TARGET_ITEMS", 8) or 8),
            "max_blocked_symbols": int(getattr(LS, "EXECUTION_LLM_REVIEW_MAX_BLOCKED_SYMBOLS", 3) or 3),
            "allow_reduce_only": bool(getattr(LS, "EXECUTION_LLM_REVIEW_ALLOW_REDUCE_ONLY", True)),
            "turnover_multiplier_floor": float(getattr(LS, "EXECUTION_LLM_REVIEW_TURNOVER_MULTIPLIER_FLOOR", 0.6) or 0.6),
            "turnover_multiplier_cap": float(getattr(LS, "EXECUTION_LLM_REVIEW_TURNOVER_MULTIPLIER_CAP", 1.15) or 1.15),
            "artifact_root": str(getattr(LS, "EXECUTION_LLM_REVIEW_ARTIFACT_ROOT", Path(LS.TRADE_CLOCK_ROOT) / "llm_execution_review")),
        },
        "trade_release": {
            "enabled": bool(getattr(LS, "ENABLE_TRADE_RELEASE", True)),
            "valid_after_time": str(getattr(LS, "TRADE_RELEASE_VALID_AFTER_TIME", "09:30:30") or "09:30:30"),
            "expires_at_time": str(getattr(LS, "TRADE_RELEASE_EXPIRES_AT_TIME", "15:00:00") or "15:00:00"),
            "calendar_lookback_days": int(getattr(LS, "TRADE_RELEASE_CALENDAR_LOOKBACK_DAYS", 7) or 7),
            "calendar_forward_days": int(getattr(LS, "TRADE_RELEASE_CALENDAR_FORWARD_DAYS", 45) or 45),
        },
        "trade_clock": {
            "enabled": bool(getattr(LS, "ENABLE_TRADE_CLOCK", True)),
            "timezone": str(getattr(LS, "TRADE_CLOCK_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"),
            "poll_seconds": int(getattr(LS, "TRADE_CLOCK_POLL_SECONDS", 30) or 30),
            "remote_delegate": {
                "enabled": bool(getattr(LS, "ENABLE_TRADE_CLOCK_REMOTE_DELEGATE", False)),
                "remote_user": str(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_REMOTE_USER", "ubuntu") or "ubuntu"),
                "remote_host": str(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_REMOTE_HOST", "43.129.28.141") or "43.129.28.141"),
                "remote_repo_root": str(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_REMOTE_REPO_ROOT", "/opt/ashare_runtime") or "/opt/ashare_runtime"),
                "python_executable": str(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_PYTHON", "/usr/bin/python3") or "/usr/bin/python3"),
                "phases": list(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_PHASES", ["research_refresh", "release_refresh", "summary"]) or []),
                "fallback_to_local": bool(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_FALLBACK_TO_LOCAL", True)),
                "ssh_options": list(getattr(LS, "TRADE_CLOCK_REMOTE_DELEGATE_SSH_OPTIONS", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]) or []),
            },
            "account_snapshot": {
                "enabled": bool(getattr(LS, "CLOCK_ACCOUNT_SNAPSHOT_ENABLED", True)),
                "concentration_top1_high": float(getattr(LS, "CLOCK_CONCENTRATION_TOP1_HIGH", 0.35) or 0.35),
                "concentration_top1_elevated": float(getattr(LS, "CLOCK_CONCENTRATION_TOP1_ELEVATED", 0.22) or 0.22),
                "concentration_hhi_high": float(getattr(LS, "CLOCK_CONCENTRATION_HHI_HIGH", 0.22) or 0.22),
                "concentration_hhi_elevated": float(getattr(LS, "CLOCK_CONCENTRATION_HHI_ELEVATED", 0.15) or 0.15),
            },
            "runtime_hot_reload": {
                "enabled": bool(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_ENABLED", False)),
                "check_interval_seconds": int(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_CHECK_INTERVAL_SECONDS", 20) or 20),
                "watch_scripts_root": str(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_SCRIPTS_ROOT", Path(LS.REPO_ROOT) / "scripts")),
                "watch_hub_root": str(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_HUB_ROOT", Path(LS.PROJECT_ROOT) / "engine")),
                "watch_bridge_root": str(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_BRIDGE_ROOT", Path(LS.PROJECT_ROOT).parents[0] / "live_execution_bridge")),
                "watch_csharp_root": str(getattr(LS, "TRADE_CLOCK_RUNTIME_HOT_RELOAD_CSHARP_ROOT", Path(LS.REPO_ROOT) / "csharp_runtime_skeleton")),
            },
            "execution_windows": list(
                getattr(
                    LS,
                    "TRADE_CLOCK_EXECUTION_WINDOWS",
                    [{"label": "morning_primary", "start": "09:30:30", "end": "10:00:00"}],
                )
                or [{"label": "morning_primary", "start": "09:30:30", "end": "10:00:00"}]
            ),
            "scheduler": {
                "enabled": bool(getattr(LS, "TRADE_CLOCK_SCHEDULER_ENABLED", True)),
                "profile": str(getattr(LS, "TRADE_CLOCK_SCHEDULER_PROFILE", "daily_production") or "daily_production"),
                "log_tail_lines": int(getattr(LS, "TRADE_CLOCK_SCHEDULER_LOG_TAIL_LINES", 30) or 30),
                "research_refresh_profile": str(
                    getattr(
                        LS,
                        "TRADE_CLOCK_RESEARCH_REFRESH_PROFILE",
                        getattr(LS, "TRADE_CLOCK_SCHEDULER_PROFILE", "daily_production"),
                    )
                    or getattr(LS, "TRADE_CLOCK_SCHEDULER_PROFILE", "daily_production")
                ),
                "release_refresh_profile": str(getattr(LS, "TRADE_CLOCK_RELEASE_REFRESH_PROFILE", "daily_production") or "daily_production"),
                "fallback_max_portfolio_age_hours": int(getattr(LS, "TRADE_CLOCK_FALLBACK_MAX_PORTFOLIO_AGE_HOURS", 96) or 96),
                "fallback_require_release": bool(getattr(LS, "TRADE_CLOCK_FALLBACK_REQUIRE_RELEASE", False)),
                "simulation_namespace": str(getattr(LS, "TRADE_CLOCK_SIMULATION_NAMESPACE", "precision") or "precision"),
                "shadow_namespace": str(getattr(LS, "TRADE_CLOCK_SHADOW_NAMESPACE", "shadow") or "shadow"),
                "shadow_enabled": bool(getattr(LS, "TRADE_CLOCK_SHADOW_ENABLED", False)),
                "afternoon_shadow_enabled": bool(getattr(LS, "TRADE_CLOCK_AFTERNOON_SHADOW_ENABLED", False)),
                "simulation_execution_mode": str(getattr(LS, "TRADE_CLOCK_SIMULATION_EXECUTION_MODE", "precision") or "precision"),
                "shadow_execution_mode": str(getattr(LS, "TRADE_CLOCK_SHADOW_EXECUTION_MODE", "precision") or "precision"),
                "simulation_precision_trade": bool(getattr(LS, "TRADE_CLOCK_SIMULATION_PRECISION_TRADE", True)),
                "shadow_precision_trade": bool(getattr(LS, "TRADE_CLOCK_SHADOW_PRECISION_TRADE", True)),
                "simulation_ignore_market_panic_reduce_only": bool(getattr(LS, "TRADE_CLOCK_SIMULATION_IGNORE_MARKET_PANIC_REDUCE_ONLY", True)),
                "shadow_ignore_market_panic_reduce_only": bool(getattr(LS, "TRADE_CLOCK_SHADOW_IGNORE_MARKET_PANIC_REDUCE_ONLY", True)),
                "simulation_allow_unfinished_orders_reconcile": bool(getattr(LS, "TRADE_CLOCK_SIMULATION_ALLOW_UNFINISHED_ORDERS_RECONCILE", False)),
                "shadow_allow_unfinished_orders_reconcile": bool(getattr(LS, "TRADE_CLOCK_SHADOW_ALLOW_UNFINISHED_ORDERS_RECONCILE", False)),
                "morning_research_refresh_enabled": bool(getattr(LS, "TRADE_CLOCK_MORNING_RESEARCH_REFRESH_ENABLED", True)),
                "morning_release_refresh_enabled": bool(getattr(LS, "TRADE_CLOCK_MORNING_RELEASE_REFRESH_ENABLED", True)),
                "live_snapshot_refresh_enabled": bool(getattr(LS, "TRADE_CLOCK_LIVE_SNAPSHOT_REFRESH_ENABLED", True)),
                "live_snapshot_refresh_fail_open": bool(getattr(LS, "TRADE_CLOCK_LIVE_SNAPSHOT_REFRESH_FAIL_OPEN", True)),
                "live_snapshot_loop_enabled": bool(getattr(LS, "TRADE_CLOCK_LIVE_SNAPSHOT_LOOP_ENABLED", True)),
                "live_snapshot_loop_interval_seconds": int(getattr(LS, "TRADE_CLOCK_LIVE_SNAPSHOT_LOOP_INTERVAL_SECONDS", 3) or 3),
                "live_snapshot_loop_market_stages": list(
                    getattr(
                        LS,
                        "TRADE_CLOCK_LIVE_SNAPSHOT_LOOP_MARKET_STAGES",
                        ["morning_session", "afternoon_session", "closing_auction"],
                    )
                    or []
                ),
                "live_snapshot_refresh_phases": list(
                    getattr(
                        LS,
                        "TRADE_CLOCK_LIVE_SNAPSHOT_REFRESH_PHASES",
                        [
                            "preopen_gate",
                            "simulation",
                            "shadow",
                            "midday_review",
                            "afternoon_execution",
                            "afternoon_shadow",
                            "summary",
                        ],
                    )
                    or []
                ),
                "data_consistency_gate": {
                    "enabled": bool(getattr(LS, "TRADE_CLOCK_DATA_CONSISTENCY_GATE_ENABLED", True)),
                    "fail_open": bool(getattr(LS, "TRADE_CLOCK_DATA_CONSISTENCY_GATE_FAIL_OPEN", False)),
                    "max_market_age_days": int(getattr(LS, "TRADE_CLOCK_DATA_CONSISTENCY_MAX_MARKET_AGE_DAYS", 4) or 4),
                    "max_market_table_spread_days": int(getattr(LS, "TRADE_CLOCK_DATA_CONSISTENCY_MAX_MARKET_TABLE_SPREAD_DAYS", 1) or 1),
                    "require_today_refresh_phases": list(
                        getattr(
                            LS,
                            "TRADE_CLOCK_DATA_CONSISTENCY_REQUIRE_TODAY_REFRESH_PHASES",
                            ["research_refresh", "release_refresh", "preopen_gate", "simulation", "midday_review", "afternoon_execution", "summary"],
                        )
                        or []
                    ),
                    "required_today_pipelines": list(
                        getattr(
                            LS,
                            "TRADE_CLOCK_DATA_CONSISTENCY_REQUIRED_TODAY_PIPELINES",
                            ["affordable_data_refresh", "external_research_refresh", "research_fact_refresh", "industry_hard_factor_refresh"],
                        )
                        or []
                    ),
                },
                "phases": {
                    "research": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_RESEARCH_TIME", "15:05:00") or "15:05:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_RESEARCH_TIMEOUT_MINUTES", 420) or 420),
                    },
                    "release": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_RELEASE_TIME", "15:10:00") or "15:10:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_RELEASE_TIMEOUT_MINUTES", 30) or 30),
                    },
                    "research_refresh": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_RESEARCH_REFRESH_TIME", "08:35:00") or "08:35:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_RESEARCH_REFRESH_TIMEOUT_MINUTES", 15) or 15),
                    },
                    "release_refresh": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_RELEASE_REFRESH_TIME", "08:55:00") or "08:55:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_RELEASE_REFRESH_TIMEOUT_MINUTES", 10) or 10),
                    },
                    "preopen_gate": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_PREOPEN_GATE_TIME", "09:20:00") or "09:20:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_PREOPEN_GATE_TIMEOUT_MINUTES", 15) or 15),
                    },
                    "simulation": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_SIMULATION_TIME", "09:30:35") or "09:30:35"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_SIMULATION_TIMEOUT_MINUTES", 45) or 45),
                    },
                    "shadow": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_SHADOW_TIME", "09:35:00") or "09:35:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_SHADOW_TIMEOUT_MINUTES", 30) or 30),
                    },
                    "midday_review": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_MIDDAY_REVIEW_TIME", "11:35:00") or "11:35:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_MIDDAY_REVIEW_TIMEOUT_MINUTES", 10) or 10),
                    },
                    "afternoon_execution": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_AFTERNOON_EXECUTION_TIME", "13:05:00") or "13:05:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_AFTERNOON_EXECUTION_TIMEOUT_MINUTES", 30) or 30),
                    },
                    "afternoon_shadow": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_AFTERNOON_SHADOW_TIME", "13:15:00") or "13:15:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_AFTERNOON_SHADOW_TIMEOUT_MINUTES", 20) or 20),
                    },
                    "summary": {
                        "time": str(getattr(LS, "TRADE_CLOCK_PHASE_SUMMARY_TIME", "15:20:00") or "15:20:00"),
                        "timeout_minutes": int(getattr(LS, "TRADE_CLOCK_SUMMARY_TIMEOUT_MINUTES", 20) or 20),
                    },
                },
            },
        },
        "execution_bridge": {
            "enabled": LS.ENABLE_EXECUTION_BRIDGE,
            "mode": "gmtrade_sim",
            "python_executable": LS.GMTRADE_PYTHON_EXECUTABLE,
            "config_template_path": LS.GMTRADE_RUNTIME_CONFIG_TEMPLATE,
            "autogen_config_path": LS.GMTRADE_RUNTIME_AUTOGEN_PATH,
            "script_path": LS.GMTRADE_BRIDGE_SCRIPT_PATH,
            "health_probe_script_path": LS.GMTRADE_HEALTH_PROBE_SCRIPT_PATH,
        },
    }
    return cfg


def save_runtime_config(config_path: Path) -> Path:
    return _atomic_write_text(
        config_path,
        json.dumps(build_runtime_config(), ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
