from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import pandas as pd

from ..core.common import clip, normalize_symbol, parse_date, safe_float, safe_text, symbol_to_code
from ..schemas import CompanyExposure, ThemeSpec


def _split_terms(value: Any) -> List[str]:
    text = safe_text(value)
    if not text:
        return []
    parts = [item.strip() for item in text.replace(",", "|").split("|")]
    return [item for item in parts if item]


@dataclass(frozen=True)
class ThemeRegistryRuntime:
    themes: List[ThemeSpec]
    exposures: List[CompanyExposure]
    theme_lookup: Dict[str, ThemeSpec]
    themes_by_mechanism: Dict[str, List[ThemeSpec]]
    exposures_by_theme: Dict[str, List[CompanyExposure]]
    exposures_by_symbol: Dict[str, List[CompanyExposure]]
    exposures_by_company: Dict[str, List[CompanyExposure]]
    theme_keywords: Dict[str, List[str]]


def build_theme_registry_runtime(
    theme_registry_df: pd.DataFrame,
    exposure_df: pd.DataFrame,
    stock_master_df: pd.DataFrame,
) -> ThemeRegistryRuntime:
    stock_master = stock_master_df.copy()
    stock_master["ts_code"] = stock_master["ts_code"].astype(str).str.strip().str.upper()
    stock_master["symbol"] = stock_master["symbol"].astype(str).str.strip().str.upper()
    stock_index = {safe_text(row["ts_code"]): row for _, row in stock_master.iterrows()}

    themes: List[ThemeSpec] = []
    theme_lookup: Dict[str, ThemeSpec] = {}
    themes_by_mechanism: Dict[str, List[ThemeSpec]] = {}
    theme_keywords: Dict[str, List[str]] = {}
    for _, row in theme_registry_df.fillna("").iterrows():
        item = row.to_dict()
        theme = ThemeSpec(
            theme_id=safe_text(item.get("theme_id")),
            theme_name=safe_text(item.get("theme_name")),
            theme_type=safe_text(item.get("theme_type")),
            description=safe_text(item.get("description")),
            primary_chain=safe_text(item.get("primary_chain")),
            primary_data_sources=_split_terms(item.get("primary_data_sources")),
            update_frequency=safe_text(item.get("update_frequency")),
            active_flag=str(item.get("active_flag", "1")).strip() not in {"0", "false", "False"},
            mechanism_primary=safe_text(item.get("mechanism_primary")),
            default_shock_type=safe_text(item.get("default_shock_type")),
            key_terms=_split_terms(item.get("key_terms")),
        )
        themes.append(theme)
        theme_lookup[theme.theme_id] = theme
        themes_by_mechanism.setdefault(theme.mechanism_primary, []).append(theme)
        theme_keywords[theme.theme_id] = [
            token
            for token in dict.fromkeys(
                [theme.theme_name, theme.description, theme.primary_chain, *theme.key_terms]
            )
            if safe_text(token)
        ]

    exposures: List[CompanyExposure] = []
    exposures_by_theme: Dict[str, List[CompanyExposure]] = {}
    exposures_by_symbol: Dict[str, List[CompanyExposure]] = {}
    exposures_by_company: Dict[str, List[CompanyExposure]] = {}
    for _, row in exposure_df.fillna("").iterrows():
        item = row.to_dict()
        ts_code = normalize_symbol(item.get("ts_code"))
        stock_row = stock_index.get(ts_code, {})
        exposure = CompanyExposure(
            ts_code=ts_code,
            theme_id=safe_text(item.get("theme_id")),
            theme_name=safe_text(item.get("theme_name")),
            mechanism_primary=safe_text(item.get("mechanism_primary")),
            chain_position=safe_text(item.get("chain_position")),
            exposure_strength=safe_float(item.get("exposure_strength"), 0.0),
            benefit_direction=safe_text(item.get("benefit_direction")) or "long",
            purity_score=safe_float(item.get("purity_score"), 0.0),
            profit_path=safe_text(item.get("profit_path")),
            evidence_note=safe_text(item.get("evidence_note")),
            mapping_confidence=safe_float(item.get("mapping_confidence"), 0.0),
            active_flag=str(item.get("active_flag", "1")).strip() not in {"0", "false", "False"},
            symbol=safe_text(stock_row.get("symbol")) or ts_code,
            code=safe_text(stock_row.get("code")) or symbol_to_code(ts_code),
            name=safe_text(stock_row.get("name")),
            industry_primary=safe_text(stock_row.get("industry_primary")),
            subchain_primary=safe_text(stock_row.get("subchain_primary")),
        )
        exposures.append(exposure)
        exposures_by_theme.setdefault(exposure.theme_id, []).append(exposure)
        exposures_by_symbol.setdefault(exposure.ts_code, []).append(exposure)
        if exposure.name:
            exposures_by_company.setdefault(exposure.name, []).append(exposure)
        if exposure.theme_id in theme_keywords:
            theme_keywords[exposure.theme_id] = list(
                dict.fromkeys(
                    [
                        *theme_keywords[exposure.theme_id],
                        exposure.name,
                        exposure.industry_primary,
                        exposure.subchain_primary,
                        exposure.chain_position,
                        exposure.profit_path,
                        exposure.evidence_note,
                    ]
                )
            )

    return ThemeRegistryRuntime(
        themes=themes,
        exposures=exposures,
        theme_lookup=theme_lookup,
        themes_by_mechanism=themes_by_mechanism,
        exposures_by_theme=exposures_by_theme,
        exposures_by_symbol=exposures_by_symbol,
        exposures_by_company=exposures_by_company,
        theme_keywords=theme_keywords,
    )


def _event_text_blob(event: Dict[str, Any]) -> str:
    return " ".join(
        safe_text(event.get(key))
        for key in [
            "raw_title",
            "title",
            "summary",
            "event_type",
            "company_name",
            "company_name_hint",
            "security_code",
            "security_code_hint",
        ]
    ).lower()


def _extract_symbol_candidates(event: Dict[str, Any]) -> List[str]:
    candidates = []
    for key in ["symbol", "ts_code", "security_code", "security_code_hint"]:
        value = normalize_symbol(event.get(key))
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def map_event_clues_to_themes(events: List[Dict[str, Any]], registry: ThemeRegistryRuntime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, event in enumerate(events):
        event_id = safe_text(event.get("event_id")) or f"event_clue_{idx + 1}"
        text_blob = _event_text_blob(event)
        symbol_hits = _extract_symbol_candidates(event)
        company_text = " ".join(
            safe_text(event.get(key))
            for key in ["company_name", "company_name_hint", "raw_title", "title", "summary"]
        )
        theme_matches: Dict[str, Dict[str, Any]] = {}
        for symbol in symbol_hits:
            for exposure in registry.exposures_by_symbol.get(symbol, []):
                theme_matches[exposure.theme_id] = {
                    "match_score": 1.0,
                    "match_reason": f"symbol:{symbol}",
                    "matched_symbol": exposure.ts_code,
                }
        for company_name, exposures in registry.exposures_by_company.items():
            if company_name and company_name in company_text:
                for exposure in exposures:
                    current = theme_matches.get(exposure.theme_id)
                    if current and current.get("match_score", 0.0) >= 0.92:
                        continue
                    theme_matches[exposure.theme_id] = {
                        "match_score": 0.92,
                        "match_reason": f"company:{company_name}",
                        "matched_symbol": exposure.ts_code,
                    }
        for theme_id, keywords in registry.theme_keywords.items():
            hit_count = sum(1 for keyword in keywords if keyword and keyword.lower() in text_blob)
            if hit_count <= 0:
                continue
            inferred_score = clip(0.35 + 0.12 * hit_count, 0.35, 0.88)
            current = theme_matches.get(theme_id)
            if current and current.get("match_score", 0.0) >= inferred_score:
                continue
            theme_matches[theme_id] = {
                "match_score": inferred_score,
                "match_reason": f"keywords:{hit_count}",
                "matched_symbol": current.get("matched_symbol", "") if current else "",
            }

        importance = clip(safe_float(event.get("importance_score"), safe_float(event.get("importance"), 0.0)), 0.0, 1.0)
        if importance <= 0:
            importance = clip(safe_float(event.get("research_priority_score"), 0.0), 0.0, 1.0)
        evidence_quality = clip(safe_float(event.get("evidence_quality_score"), 0.5), 0.0, 1.0)
        anti_overfit = clip(safe_float(event.get("anti_overfit_weight"), 1.0), 0.0, 1.0)
        direction_text = safe_text(event.get("event_direction")).lower()
        if direction_text in {"negative", "利空", "down", "bearish"}:
            direction = "negative"
        elif direction_text in {"positive", "利好", "up", "bullish"}:
            direction = "positive"
        else:
            direction = "neutral"
        event_date = parse_date(event.get("publish_time") or event.get("crawl_time")) or parse_date(event.get("date"))
        for theme_id, info in theme_matches.items():
            theme = registry.theme_lookup.get(theme_id)
            if theme is None:
                continue
            match_score = safe_float(info.get("match_score"), 0.0)
            clue_strength = clip(0.45 * evidence_quality + 0.35 * importance + 0.20 * anti_overfit, 0.0, 1.0)
            rows.append(
                {
                    "event_id": event_id,
                    "date": event_date,
                    "theme_id": theme_id,
                    "theme_name": theme.theme_name,
                    "mechanism_primary": theme.mechanism_primary,
                    "matched_symbol": safe_text(info.get("matched_symbol")),
                    "match_score": round(match_score, 4),
                    "clue_strength": round(clue_strength * match_score, 4),
                    "direction": direction,
                    "match_reason": safe_text(info.get("match_reason")),
                    "title": safe_text(event.get("raw_title") or event.get("title"))[:160],
                    "summary": safe_text(event.get("summary"))[:240],
                    "source_type": safe_text(event.get("source_type")),
                    "event_type": safe_text(event.get("event_type")),
                    "company_name": safe_text(event.get("company_name") or event.get("company_name_hint")),
                }
            )
    return rows
