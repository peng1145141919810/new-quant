from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_date(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            return ""
    return ""


def _score_direction(value: Any) -> float:
    text = _text(value).lower()
    if text in {"positive", "tightening", "increase", "up", "bullish", "supportive"}:
        return 1.0
    if text in {"negative", "loosening", "decrease", "down", "bearish", "weak"}:
        return -1.0
    return 0.0


def resolve_research_fact_sqlite_path(config: Dict[str, Any]) -> Path:
    raw = _text(
        dict(config.get("paths", {}) or {}).get("research_fact_sqlite_path")
        or dict(config.get("research_fact_store", {}) or {}).get("sqlite_path")
    )
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parents[3] / "data" / "sql_store" / "research_fact_layers_v1.sqlite3").resolve()


def resolve_manual_event_proxy_path(config: Dict[str, Any]) -> Path:
    raw = _text(dict(config.get("paths", {}) or {}).get("manual_event_proxy_path"))
    if raw:
        return Path(raw).resolve()
    research_root = Path(str(dict(config.get("paths", {}) or {}).get("research_root", "") or "")).resolve()
    return research_root / "manual_event_proxy" / "manual_event_proxy.jsonl"


@contextmanager
def sqlite_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS field_lineage_registry (
            table_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            upstream_source TEXT NOT NULL,
            source_class TEXT NOT NULL,
            refresh_cadence TEXT NOT NULL,
            licensing_note TEXT NOT NULL,
            notes TEXT,
            PRIMARY KEY (table_name, field_name)
        );

        CREATE TABLE IF NOT EXISTS event_fact_company_actions (
            fact_id TEXT PRIMARY KEY,
            source_event_id TEXT,
            trade_date TEXT,
            event_date TEXT,
            publish_date TEXT,
            symbol TEXT,
            ts_code TEXT,
            company_name TEXT,
            event_type TEXT,
            event_subtype TEXT,
            direction TEXT,
            headline TEXT,
            raw_source_name TEXT,
            raw_source_url TEXT,
            source_class TEXT,
            source_authority TEXT,
            source_confidence REAL,
            is_issuer_disclosure INTEGER,
            is_official_source INTEGER,
            is_structured_from_text INTEGER,
            currency TEXT,
            amount_raw TEXT,
            amount_cny REAL,
            quantity_raw TEXT,
            unit_raw TEXT,
            counterparty TEXT,
            counterparty_type TEXT,
            project_name TEXT,
            region TEXT,
            industry_chain_hint TEXT,
            mechanism_hint TEXT,
            impact_horizon TEXT,
            importance_level TEXT,
            is_major_event INTEGER,
            notes TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_fact_contract_orders (
            fact_id TEXT PRIMARY KEY,
            source_event_id TEXT,
            trade_date TEXT,
            event_date TEXT,
            symbol TEXT,
            company_name TEXT,
            event_type TEXT,
            contract_type TEXT,
            tender_type TEXT,
            project_name TEXT,
            project_owner TEXT,
            counterparty TEXT,
            counterparty_is_government INTEGER,
            amount_raw TEXT,
            amount_cny REAL,
            amount_ratio_to_revenue REAL,
            is_framework_agreement INTEGER,
            is_binding_contract INTEGER,
            is_bid_award INTEGER,
            is_new_order INTEGER,
            is_backlog_related INTEGER,
            delivery_window TEXT,
            business_segment TEXT,
            mechanism_hint TEXT,
            source_name TEXT,
            source_url TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_fact_supply_chain_signals (
            fact_id TEXT PRIMARY KEY,
            source_event_id TEXT,
            trade_date TEXT,
            event_date TEXT,
            symbol TEXT,
            company_name TEXT,
            event_type TEXT,
            signal_type TEXT,
            direction TEXT,
            product_name TEXT,
            industry_name TEXT,
            capacity_change_desc TEXT,
            price_change_desc TEXT,
            shutdown_desc TEXT,
            operation_rate_desc TEXT,
            expected_duration TEXT,
            mechanism_hint TEXT,
            source_name TEXT,
            source_url TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS industry_factor_price_inventory_daily (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            industry_name TEXT,
            sub_industry_name TEXT,
            product_name TEXT,
            factor_type TEXT,
            factor_subtype TEXT,
            value_raw TEXT,
            value_num REAL,
            unit TEXT,
            direction_hint TEXT,
            source_name TEXT,
            source_url TEXT,
            source_class TEXT,
            publish_date TEXT,
            is_official_source INTEGER,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS industry_factor_operation_daily (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            industry_name TEXT,
            sub_industry_name TEXT,
            product_name TEXT,
            operation_type TEXT,
            value_raw TEXT,
            value_num REAL,
            unit TEXT,
            direction_hint TEXT,
            source_name TEXT,
            source_url TEXT,
            source_class TEXT,
            publish_date TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS industry_factor_customs_summary_daily (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            publish_date TEXT,
            region_scope TEXT,
            industry_name TEXT,
            product_name TEXT,
            import_export_flag TEXT,
            value_raw TEXT,
            value_num REAL,
            unit TEXT,
            direction_hint TEXT,
            source_name TEXT,
            source_url TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS qianzhan_indicator_daily (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            publish_date TEXT,
            platform_section TEXT,
            industry_name TEXT,
            sub_industry_name TEXT,
            indicator_name TEXT,
            indicator_category TEXT,
            value_raw TEXT,
            value_num REAL,
            unit TEXT,
            direction_hint TEXT,
            page_title TEXT,
            page_url TEXT,
            source_class TEXT,
            auth_state TEXT,
            llm_relevance_score REAL,
            llm_tags TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS qianzhan_knowledge_cards (
            card_id TEXT PRIMARY KEY,
            trade_date TEXT,
            publish_date TEXT,
            platform_section TEXT,
            page_title TEXT,
            page_url TEXT,
            industry_name TEXT,
            summary_text TEXT,
            extracted_numbers TEXT,
            source_class TEXT,
            auth_state TEXT,
            llm_tags TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ggzy_notice_index (
            notice_id TEXT PRIMARY KEY,
            trade_date TEXT,
            publish_date TEXT,
            notice_type TEXT,
            business_type TEXT,
            project_code TEXT,
            title TEXT,
            province TEXT,
            source_platform TEXT,
            detail_url TEXT,
            company_candidates TEXT,
            amount_raw TEXT,
            amount_cny REAL,
            llm_event_type TEXT,
            llm_mechanism_hint TEXT,
            llm_relevance_score REAL,
            llm_candidate_symbols TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS intraday_proxy_quote_snapshot (
            snapshot_id TEXT PRIMARY KEY,
            trade_date TEXT,
            snapshot_time TEXT,
            symbol TEXT,
            ts_code TEXT,
            name TEXT,
            price REAL,
            open REAL,
            high REAL,
            low REAL,
            pre_close REAL,
            bid1 REAL,
            ask1 REAL,
            volume REAL,
            amount REAL,
            source_name TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS intraday_proxy_list_snapshot (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            snapshot_time TEXT,
            symbol TEXT,
            ts_code TEXT,
            name TEXT,
            price REAL,
            pct_change REAL,
            amplitude REAL,
            volume_ratio REAL,
            turnover_rate REAL,
            total_mv REAL,
            circ_mv REAL,
            rank_bucket TEXT,
            source_name TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS intraday_proxy_tick_summary (
            row_id TEXT PRIMARY KEY,
            trade_date TEXT,
            snapshot_time TEXT,
            symbol TEXT,
            ts_code TEXT,
            n_ticks INTEGER,
            buy_amount REAL,
            sell_amount REAL,
            neutral_amount REAL,
            latest_price REAL,
            source_name TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS account_truth_snapshot (
            snapshot_id TEXT PRIMARY KEY,
            trade_date TEXT,
            snapshot_time TEXT,
            account_id TEXT,
            account_mode TEXT,
            namespace TEXT,
            nav REAL,
            total_asset REAL,
            cash REAL,
            available_cash REAL,
            frozen_cash REAL,
            positions_count INTEGER,
            sellable_positions_count INTEGER,
            pending_orders_count INTEGER,
            unfinished_orders_count INTEGER,
            t1_locked_positions_count INTEGER,
            source_name TEXT,
            source_class TEXT,
            raw_payload_path TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS source_fetch_run_log (
            log_id TEXT PRIMARY KEY,
            run_id TEXT,
            pipeline_name TEXT NOT NULL,
            dataset_name TEXT,
            source_id TEXT,
            source_name TEXT,
            source_url TEXT,
            source_domain TEXT,
            trade_date TEXT,
            publish_date TEXT,
            status TEXT NOT NULL,
            rows_written INTEGER NOT NULL DEFAULT 0,
            items_seen INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            error_class TEXT,
            message TEXT,
            artifact_path TEXT,
            params_json TEXT,
            extra_json TEXT,
            is_stale INTEGER NOT NULL DEFAULT 0,
            freshness_days INTEGER,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_event_fact_company_symbol_date
            ON event_fact_company_actions (symbol, publish_date, trade_date);
        CREATE INDEX IF NOT EXISTS idx_event_fact_contract_symbol_date
            ON event_fact_contract_orders (symbol, event_date, trade_date);
        CREATE INDEX IF NOT EXISTS idx_event_fact_supply_symbol_date
            ON event_fact_supply_chain_signals (symbol, event_date, trade_date);
        CREATE INDEX IF NOT EXISTS idx_price_inventory_date_industry
            ON industry_factor_price_inventory_daily (trade_date, industry_name, product_name);
        CREATE INDEX IF NOT EXISTS idx_operation_date_industry
            ON industry_factor_operation_daily (trade_date, industry_name, product_name);
        CREATE INDEX IF NOT EXISTS idx_customs_date_industry
            ON industry_factor_customs_summary_daily (trade_date, industry_name, product_name);
        CREATE INDEX IF NOT EXISTS idx_qianzhan_indicator_date_industry
            ON qianzhan_indicator_daily (trade_date, industry_name, indicator_name);
        CREATE INDEX IF NOT EXISTS idx_qianzhan_cards_date_industry
            ON qianzhan_knowledge_cards (trade_date, industry_name, platform_section);
        CREATE INDEX IF NOT EXISTS idx_ggzy_notice_date_type
            ON ggzy_notice_index (trade_date, notice_type, business_type);
        CREATE INDEX IF NOT EXISTS idx_intraday_quote_trade_symbol
            ON intraday_proxy_quote_snapshot (trade_date, symbol, snapshot_time);
        CREATE INDEX IF NOT EXISTS idx_intraday_list_trade_symbol
            ON intraday_proxy_list_snapshot (trade_date, symbol, snapshot_time);
        CREATE INDEX IF NOT EXISTS idx_intraday_tick_trade_symbol
            ON intraday_proxy_tick_summary (trade_date, symbol, snapshot_time);
        CREATE INDEX IF NOT EXISTS idx_account_truth_trade_account
            ON account_truth_snapshot (trade_date, account_id, snapshot_time);
        CREATE INDEX IF NOT EXISTS idx_source_fetch_trade_pipeline
            ON source_fetch_run_log (trade_date, pipeline_name, dataset_name, source_id);
        CREATE INDEX IF NOT EXISTS idx_source_fetch_started_at
            ON source_fetch_run_log (started_at, pipeline_name);
        """
    )


_LINEAGE_TABLE_FIELDS: Dict[str, Sequence[str]] = {
    "event_fact_company_actions": (
        "fact_id",
        "source_event_id",
        "trade_date",
        "event_date",
        "publish_date",
        "symbol",
        "ts_code",
        "company_name",
        "event_type",
        "event_subtype",
        "direction",
        "headline",
        "raw_source_name",
        "raw_source_url",
        "source_class",
        "source_authority",
        "source_confidence",
        "is_issuer_disclosure",
        "is_official_source",
        "is_structured_from_text",
        "currency",
        "amount_raw",
        "amount_cny",
        "quantity_raw",
        "unit_raw",
        "counterparty",
        "counterparty_type",
        "project_name",
        "region",
        "industry_chain_hint",
        "mechanism_hint",
        "impact_horizon",
        "importance_level",
        "is_major_event",
        "notes",
        "raw_payload_path",
        "ingested_at",
    ),
    "event_fact_contract_orders": (
        "fact_id",
        "source_event_id",
        "trade_date",
        "event_date",
        "symbol",
        "company_name",
        "event_type",
        "contract_type",
        "tender_type",
        "project_name",
        "project_owner",
        "counterparty",
        "counterparty_is_government",
        "amount_raw",
        "amount_cny",
        "amount_ratio_to_revenue",
        "is_framework_agreement",
        "is_binding_contract",
        "is_bid_award",
        "is_new_order",
        "is_backlog_related",
        "delivery_window",
        "business_segment",
        "mechanism_hint",
        "source_name",
        "source_url",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "event_fact_supply_chain_signals": (
        "fact_id",
        "source_event_id",
        "trade_date",
        "event_date",
        "symbol",
        "company_name",
        "event_type",
        "signal_type",
        "direction",
        "product_name",
        "industry_name",
        "capacity_change_desc",
        "price_change_desc",
        "shutdown_desc",
        "operation_rate_desc",
        "expected_duration",
        "mechanism_hint",
        "source_name",
        "source_url",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "industry_factor_price_inventory_daily": (
        "row_id",
        "trade_date",
        "industry_name",
        "sub_industry_name",
        "product_name",
        "factor_type",
        "factor_subtype",
        "value_raw",
        "value_num",
        "unit",
        "direction_hint",
        "source_name",
        "source_url",
        "source_class",
        "publish_date",
        "is_official_source",
        "raw_payload_path",
        "ingested_at",
    ),
    "industry_factor_operation_daily": (
        "row_id",
        "trade_date",
        "industry_name",
        "sub_industry_name",
        "product_name",
        "operation_type",
        "value_raw",
        "value_num",
        "unit",
        "direction_hint",
        "source_name",
        "source_url",
        "source_class",
        "publish_date",
        "raw_payload_path",
        "ingested_at",
    ),
    "industry_factor_customs_summary_daily": (
        "row_id",
        "trade_date",
        "publish_date",
        "region_scope",
        "industry_name",
        "product_name",
        "import_export_flag",
        "value_raw",
        "value_num",
        "unit",
        "direction_hint",
        "source_name",
        "source_url",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "qianzhan_indicator_daily": (
        "row_id",
        "trade_date",
        "publish_date",
        "platform_section",
        "industry_name",
        "sub_industry_name",
        "indicator_name",
        "indicator_category",
        "value_raw",
        "value_num",
        "unit",
        "direction_hint",
        "page_title",
        "page_url",
        "source_class",
        "auth_state",
        "llm_relevance_score",
        "llm_tags",
        "raw_payload_path",
        "ingested_at",
    ),
    "qianzhan_knowledge_cards": (
        "card_id",
        "trade_date",
        "publish_date",
        "platform_section",
        "page_title",
        "page_url",
        "industry_name",
        "summary_text",
        "extracted_numbers",
        "source_class",
        "auth_state",
        "llm_tags",
        "raw_payload_path",
        "ingested_at",
    ),
    "ggzy_notice_index": (
        "notice_id",
        "trade_date",
        "publish_date",
        "notice_type",
        "business_type",
        "project_code",
        "title",
        "province",
        "source_platform",
        "detail_url",
        "company_candidates",
        "amount_raw",
        "amount_cny",
        "llm_event_type",
        "llm_mechanism_hint",
        "llm_relevance_score",
        "llm_candidate_symbols",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "intraday_proxy_quote_snapshot": (
        "snapshot_id",
        "trade_date",
        "snapshot_time",
        "symbol",
        "ts_code",
        "name",
        "price",
        "open",
        "high",
        "low",
        "pre_close",
        "bid1",
        "ask1",
        "volume",
        "amount",
        "source_name",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "intraday_proxy_list_snapshot": (
        "row_id",
        "trade_date",
        "snapshot_time",
        "symbol",
        "ts_code",
        "name",
        "price",
        "pct_change",
        "amplitude",
        "turnover_rate",
        "volume_ratio",
        "total_mv",
        "circ_mv",
        "rank_bucket",
        "source_name",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "intraday_proxy_tick_summary": (
        "row_id",
        "trade_date",
        "snapshot_time",
        "symbol",
        "ts_code",
        "n_ticks",
        "buy_amount",
        "sell_amount",
        "neutral_amount",
        "latest_price",
        "source_name",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
    "account_truth_snapshot": (
        "snapshot_id",
        "trade_date",
        "snapshot_time",
        "account_id",
        "account_mode",
        "namespace",
        "nav",
        "total_asset",
        "cash",
        "available_cash",
        "frozen_cash",
        "positions_count",
        "sellable_positions_count",
        "pending_orders_count",
        "unfinished_orders_count",
        "t1_locked_positions_count",
        "source_name",
        "source_class",
        "raw_payload_path",
        "ingested_at",
    ),
}


def _lineage_note(field_name: str) -> str:
    if field_name in {"source_class", "raw_source_name", "raw_source_url", "source_authority"}:
        return "row-level lineage anchor; actual upstream authority is determined per row, not fixed at table level"
    if field_name in {"amount_cny", "amount_ratio_to_revenue", "mechanism_hint", "importance_level", "direction_hint"}:
        return "research-side standardized field; do not treat as canonical truth without row-level source review"
    if field_name in {"auth_state", "detail_status", "source_platform"}:
        return "capture-side operational metadata; indicates how the page was accessed or whether detail enrichment succeeded"
    if field_name == "raw_payload_path":
        return "points to the raw json/jsonl record or dataset path used during research-side structuring"
    return "research-side field; see row-level source columns for actual authority"


def register_default_field_lineage(conn: sqlite3.Connection) -> None:
    defaults = {
        "event_fact_company_actions": (
            "issuer disclosures / exchange pages / government procurement pages / affordable low-cost mirrors / manual research proxy",
            "mixed_research_row_level",
            "daily + on-demand",
            "research-only structured fact layer; do not promote directly into canonical truth",
        ),
        "event_fact_contract_orders": (
            "issuer disclosures / government procurement pages / public tender pages",
            "mixed_research_row_level",
            "daily + on-demand",
            "research-only contract/order fact layer; row-level source columns determine authority",
        ),
        "event_fact_supply_chain_signals": (
            "issuer disclosures / public commodity pages / public official pages / manual research proxy",
            "mixed_research_row_level",
            "daily + on-demand",
            "research-only supply-chain signal layer; heuristic structuring is explicitly non-canonical",
        ),
        "industry_factor_price_inventory_daily": (
            "Tushare low-cost futures mirror / 100ppi public pages / official statistics pages",
            "mixed_research_row_level",
            "daily",
            "research-only price/inventory layer; combines official, exchange-like, and public-web sources",
        ),
        "industry_factor_operation_daily": (
            "official industry operation pages / official statistics pages / research-side parsers",
            "mixed_research_row_level",
            "daily + monthly",
            "research-only operation layer; row-level source_class distinguishes official and non-official inputs",
        ),
        "industry_factor_customs_summary_daily": (
            "gov.cn customs summary pages",
            "official_truth",
            "monthly + summary refresh",
            "research-only normalization of official customs summary pages; not HS-code detail truth",
        ),
        "qianzhan_indicator_daily": (
            "qianzhan member pages / chart pages / industry database pages",
            "mixed_research_row_level",
            "daily",
            "member-page extraction for research use; do not treat as canonical truth without page-level review",
        ),
        "qianzhan_knowledge_cards": (
            "qianzhan chart pages / policy pages / stock data pages / industry analysis pages",
            "mixed_research_row_level",
            "daily",
            "research-side knowledge card layer from member pages; narrative extraction is explicitly non-canonical",
        ),
        "ggzy_notice_index": (
            "ggzy.gov.cn public notice pages / linked provincial public-resource platforms",
            "mixed_research_row_level",
            "daily + intraday discovery",
            "public notice discovery index for research/event routing; detail completeness varies by upstream page",
        ),
        "intraday_proxy_quote_snapshot": (
            "tushare realtime_quote crawler endpoint",
            "proxy_intraday_truth",
            "intraday",
            "highest-priority intraday proxy snapshot; not broker/account truth",
        ),
        "intraday_proxy_list_snapshot": (
            "tushare realtime_list crawler endpoint",
            "proxy_intraday_truth",
            "intraday",
            "highest-priority market breadth proxy snapshot; not exchange canonical truth",
        ),
        "intraday_proxy_tick_summary": (
            "tushare realtime_tick crawler endpoint",
            "proxy_intraday_truth",
            "intraday",
            "intraday tick-summary proxy layer; use as execution/research proxy, not authoritative tape truth",
        ),
        "account_truth_snapshot": (
            "broker health snapshot / OMS actual portfolio snapshot / pending-order ledgers",
            "derived_truth_bridge",
            "intraday",
            "runtime account-truth bridge normalized from broker and OMS artifacts",
        ),
    }
    rows: List[tuple[str, str, str, str, str, str, str]] = []
    for table_name, fields in _LINEAGE_TABLE_FIELDS.items():
        upstream, source_class, cadence, licensing = defaults[table_name]
        for field_name in fields:
            rows.append(
                (
                    table_name,
                    field_name,
                    upstream,
                    source_class,
                    cadence,
                    licensing,
                    _lineage_note(field_name),
                )
            )
    conn.executemany(
        """
        INSERT INTO field_lineage_registry (
            table_name, field_name, upstream_source, source_class, refresh_cadence, licensing_note, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(table_name, field_name) DO UPDATE SET
            upstream_source=excluded.upstream_source,
            source_class=excluded.source_class,
            refresh_cadence=excluded.refresh_cadence,
            licensing_note=excluded.licensing_note,
            notes=excluded.notes
        """,
        rows,
    )


def upsert_rows(conn: sqlite3.Connection, table_name: str, rows: Iterable[Dict[str, Any]], key_columns: Sequence[str]) -> int:
    payload = [dict(item) for item in rows if isinstance(item, dict)]
    if not payload:
        return 0
    columns = sorted({key for item in payload for key in item.keys()})
    update_cols = [col for col in columns if col not in key_columns]
    sql = (
        f"INSERT INTO {table_name} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT({', '.join(key_columns)}) DO UPDATE SET "
        + ", ".join(f"{col}=excluded.{col}" for col in update_cols)
    )
    conn.executemany(sql, [[item.get(col, "") for col in columns] for item in payload])
    return len(payload)


def insert_source_fetch_logs(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    payload = [dict(item) for item in rows if isinstance(item, dict)]
    if not payload:
        return 0
    return upsert_rows(conn, "source_fetch_run_log", payload, ("log_id",))


def fetch_rows(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    where_sql: str = "",
    params: Sequence[Any] = (),
    order_by: str = "",
) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {table_name}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def load_best_symbol_event_facts(
    config: Dict[str, Any],
    *,
    symbols: Iterable[str],
    as_of_date: str = "",
    lookback_days: int = 45,
) -> Dict[str, Dict[str, Any]]:
    wanted = [str(item or "").strip().upper() for item in symbols if str(item or "").strip()]
    if not wanted:
        return {}
    db_path = resolve_research_fact_sqlite_path(config)
    if not db_path.exists():
        return {}
    parsed_as_of = _parse_date(as_of_date)
    cutoff = ""
    if parsed_as_of:
        cutoff = (datetime.strptime(parsed_as_of, "%Y-%m-%d") - timedelta(days=max(1, int(lookback_days or 45)))).strftime("%Y-%m-%d")
    placeholders = ", ".join("?" for _ in wanted)
    best: Dict[str, Dict[str, Any]] = {}
    with sqlite_connection(db_path) as conn:
        rows = fetch_rows(
            conn,
            "event_fact_company_actions",
            where_sql=f"upper(symbol) IN ({placeholders}) AND (publish_date = '' OR publish_date IS NULL OR publish_date >= ?)",
            params=[*wanted, cutoff],
            order_by="""
                is_major_event DESC,
                CASE importance_level
                    WHEN 'critical' THEN 4
                    WHEN 'high' THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 1
                    ELSE 0
                END DESC,
                source_confidence DESC,
                publish_date DESC,
                event_date DESC,
                trade_date DESC
            """,
        )
        for row in rows:
            symbol = _text(row.get("symbol")).upper()
            if symbol and symbol not in best:
                best[symbol] = row
    return best


def _latest_rows_within(rows: List[Dict[str, Any]], as_of_date: str, lookback_days: int) -> List[Dict[str, Any]]:
    parsed_as_of = _parse_date(as_of_date)
    if not parsed_as_of:
        return list(rows)
    upper = datetime.strptime(parsed_as_of, "%Y-%m-%d").date()
    lower = upper - timedelta(days=max(1, int(lookback_days or 1)))
    out: List[Dict[str, Any]] = []
    for row in rows:
        date_text = _parse_date(row.get("trade_date") or row.get("publish_date") or row.get("event_date"))
        if not date_text:
            continue
        try:
            current = datetime.strptime(date_text, "%Y-%m-%d").date()
        except Exception:
            continue
        if lower <= current <= upper:
            out.append(row)
    return out


def load_router_factor_context(config: Dict[str, Any], *, as_of_date: str) -> Dict[str, Any]:
    db_path = resolve_research_fact_sqlite_path(config)
    if not db_path.exists():
        return {}
    with sqlite_connection(db_path) as conn:
        price_rows = fetch_rows(conn, "industry_factor_price_inventory_daily", where_sql="trade_date <> ''", order_by="trade_date ASC")
        operation_rows = fetch_rows(conn, "industry_factor_operation_daily", where_sql="trade_date <> ''", order_by="trade_date ASC")
        customs_rows = fetch_rows(conn, "industry_factor_customs_summary_daily", where_sql="trade_date <> ''", order_by="trade_date ASC")
    return {
        "price_inventory": _aggregate_price_inventory_context(_latest_rows_within(price_rows, as_of_date, 10)),
        "trend_capex": _aggregate_trend_capex_context(
            _latest_rows_within(operation_rows, as_of_date, 45),
            _latest_rows_within(customs_rows, as_of_date, 120),
        ),
    }


def _empty_price_bucket() -> Dict[str, Any]:
    return {"price_scores": [], "inventory_scores": [], "trade_scores": [], "evidence_count": 0, "top_products": []}


def _finalize_price_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    price_scores = [float(item) for item in list(bucket.get("price_scores", []) or [])]
    inventory_scores = [float(item) for item in list(bucket.get("inventory_scores", []) or [])]
    trade_scores = [float(item) for item in list(bucket.get("trade_scores", []) or [])]
    return {
        "price_momentum_score": round(sum(price_scores) / max(len(price_scores), 1), 4) if price_scores else 0.0,
        "inventory_tightness_score": round(sum(inventory_scores) / max(len(inventory_scores), 1), 4) if inventory_scores else 0.0,
        "trade_flow_verification_score": round(sum(trade_scores) / max(len(trade_scores), 1), 4) if trade_scores else 0.0,
        "source_consensus_score": round(
            (sum(price_scores) + sum(inventory_scores) + sum(trade_scores))
            / max(len(price_scores) + len(inventory_scores) + len(trade_scores), 1),
            4,
        ),
        "source_state_score": round(
            0.45 * (sum(price_scores) / max(len(price_scores), 1) if price_scores else 0.0)
            + 0.35 * (sum(inventory_scores) / max(len(inventory_scores), 1) if inventory_scores else 0.0)
            + 0.20 * (sum(trade_scores) / max(len(trade_scores), 1) if trade_scores else 0.0),
            4,
        ),
        "confidence": round(min(1.0, 0.35 + 0.08 * int(bucket.get("evidence_count", 0) or 0)), 4),
        "source_count": int(bucket.get("evidence_count", 0) or 0),
        "top_products": list(dict.fromkeys(list(bucket.get("top_products", []) or [])))[:6],
    }


def _aggregate_price_inventory_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        date_text = _parse_date(row.get("trade_date") or row.get("publish_date"))
        if not date_text:
            continue
        industry = _text(row.get("industry_name")) or "price_inventory"
        bucket = by_date.setdefault(date_text, {"overall": _finalize_price_bucket(_empty_price_bucket()), "by_industry": {}, "highlights": []})
        industry_bucket = bucket["by_industry"].setdefault(industry, _empty_price_bucket())
        score = _score_direction(row.get("direction_hint"))
        factor_type = _text(row.get("factor_type"))
        if factor_type in {"spot_price", "futures_price", "price_digest", "official_price_index"}:
            industry_bucket["price_scores"].append(score)
        if factor_type in {"inventory", "warehouse_receipt"}:
            industry_bucket["inventory_scores"].append(score)
        if factor_type in {"trade_flow", "customs_trade"}:
            industry_bucket["trade_scores"].append(score)
        industry_bucket["evidence_count"] += 1
        product = _text(row.get("product_name"))
        if product:
            industry_bucket["top_products"].append(product)
        if len(bucket["highlights"]) < 10:
            bucket["highlights"].append(
                {
                    "industry_name": industry,
                    "product_name": product,
                    "factor_type": factor_type,
                    "factor_subtype": _text(row.get("factor_subtype")),
                    "direction_hint": _text(row.get("direction_hint")),
                    "value_raw": _text(row.get("value_raw"))[:180],
                    "source_name": _text(row.get("source_name")),
                }
            )
    for bucket in by_date.values():
        merged = _empty_price_bucket()
        for industry_bucket in bucket["by_industry"].values():
            merged["price_scores"].extend(list(industry_bucket.get("price_scores", []) or []))
            merged["inventory_scores"].extend(list(industry_bucket.get("inventory_scores", []) or []))
            merged["trade_scores"].extend(list(industry_bucket.get("trade_scores", []) or []))
            merged["evidence_count"] += int(industry_bucket.get("evidence_count", 0) or 0)
            merged["top_products"].extend(list(industry_bucket.get("top_products", []) or []))
        bucket["overall"] = _finalize_price_bucket(merged)
        for industry, industry_bucket in list(bucket["by_industry"].items()):
            bucket["by_industry"][industry] = _finalize_price_bucket(industry_bucket)
    return {"by_date": by_date}


def _empty_trend_bucket() -> Dict[str, Any]:
    return {"industry_scores": [], "demand_scores": [], "external_scores": [], "evidence_count": 0, "top_products": []}


def _finalize_trend_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    industry_scores = [float(item) for item in list(bucket.get("industry_scores", []) or [])]
    demand_scores = [float(item) for item in list(bucket.get("demand_scores", []) or [])]
    external_scores = [float(item) for item in list(bucket.get("external_scores", []) or [])]
    return {
        "industry_expansion_score": round(sum(industry_scores) / max(len(industry_scores), 1), 4) if industry_scores else 0.0,
        "demand_verification_score": round(sum(demand_scores) / max(len(demand_scores), 1), 4) if demand_scores else 0.0,
        "external_demand_score": round(sum(external_scores) / max(len(external_scores), 1), 4) if external_scores else 0.0,
        "source_consensus_score": round(
            (sum(industry_scores) + sum(demand_scores) + sum(external_scores))
            / max(len(industry_scores) + len(demand_scores) + len(external_scores), 1),
            4,
        ),
        "source_state_score": round(
            0.42 * (sum(industry_scores) / max(len(industry_scores), 1) if industry_scores else 0.0)
            + 0.33 * (sum(demand_scores) / max(len(demand_scores), 1) if demand_scores else 0.0)
            + 0.25 * (sum(external_scores) / max(len(external_scores), 1) if external_scores else 0.0),
            4,
        ),
        "confidence": round(min(1.0, 0.35 + 0.08 * int(bucket.get("evidence_count", 0) or 0)), 4),
        "source_count": int(bucket.get("evidence_count", 0) or 0),
        "top_products": list(dict.fromkeys(list(bucket.get("top_products", []) or [])))[:6],
    }


def _aggregate_trend_capex_context(operation_rows: List[Dict[str, Any]], customs_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in operation_rows:
        date_text = _parse_date(row.get("trade_date") or row.get("publish_date"))
        if not date_text:
            continue
        industry = _text(row.get("industry_name")) or "trend_capex"
        bucket = by_date.setdefault(date_text, {"overall": _finalize_trend_bucket(_empty_trend_bucket()), "by_industry": {}, "highlights": []})
        industry_bucket = bucket["by_industry"].setdefault(industry, _empty_trend_bucket())
        score = _score_direction(row.get("direction_hint"))
        op_type = _text(row.get("operation_type"))
        if op_type in {"investment_growth", "capacity_expansion", "project_progress", "capacity_utilization"}:
            industry_bucket["industry_scores"].append(score)
        if op_type in {"demand_growth", "order_growth", "procurement"}:
            industry_bucket["demand_scores"].append(score)
        if op_type in {"export_growth", "external_demand"}:
            industry_bucket["external_scores"].append(score)
        industry_bucket["evidence_count"] += 1
        product = _text(row.get("product_name"))
        if product:
            industry_bucket["top_products"].append(product)
        if len(bucket["highlights"]) < 10:
            bucket["highlights"].append(
                {
                    "industry_name": industry,
                    "product_name": product,
                    "operation_type": op_type,
                    "direction_hint": _text(row.get("direction_hint")),
                    "value_raw": _text(row.get("value_raw"))[:180],
                    "source_name": _text(row.get("source_name")),
                }
            )
    for row in customs_rows:
        date_text = _parse_date(row.get("trade_date") or row.get("publish_date"))
        if not date_text:
            continue
        industry = _text(row.get("industry_name")) or "trend_capex"
        bucket = by_date.setdefault(date_text, {"overall": _finalize_trend_bucket(_empty_trend_bucket()), "by_industry": {}, "highlights": []})
        industry_bucket = bucket["by_industry"].setdefault(industry, _empty_trend_bucket())
        score = _score_direction(row.get("direction_hint"))
        industry_bucket["external_scores"].append(score)
        industry_bucket["evidence_count"] += 1
        product = _text(row.get("product_name"))
        if product:
            industry_bucket["top_products"].append(product)
    for bucket in by_date.values():
        merged = _empty_trend_bucket()
        for industry_bucket in bucket["by_industry"].values():
            merged["industry_scores"].extend(list(industry_bucket.get("industry_scores", []) or []))
            merged["demand_scores"].extend(list(industry_bucket.get("demand_scores", []) or []))
            merged["external_scores"].extend(list(industry_bucket.get("external_scores", []) or []))
            merged["evidence_count"] += int(industry_bucket.get("evidence_count", 0) or 0)
            merged["top_products"].extend(list(industry_bucket.get("top_products", []) or []))
        bucket["overall"] = _finalize_trend_bucket(merged)
        for industry, industry_bucket in list(bucket["by_industry"].items()):
            bucket["by_industry"][industry] = _finalize_trend_bucket(industry_bucket)
    return {"by_date": by_date}
