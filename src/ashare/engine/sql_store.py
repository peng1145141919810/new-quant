from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

import pandas as pd


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def resolve_sqlite_path(config: Dict[str, Any]) -> Path:
    raw = _safe_text(
        dict(config.get("data_store", {}) or {}).get("sqlite_path")
        or dict(config.get("paths", {}) or {}).get("data_sqlite_path")
    )
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parents[3] / "data" / "sql_store" / "research_data_v1.sqlite3").resolve()


def sql_store_enabled(config: Dict[str, Any]) -> bool:
    return bool(dict(config.get("data_store", {}) or {}).get("enabled", False))


def sql_store_prefer_router(config: Dict[str, Any]) -> bool:
    return bool(dict(config.get("data_store", {}) or {}).get("prefer_sql_for_router", False))


def mirror_runtime_json_artifact(config: Dict[str, Any], path: str | Path, payload: Dict[str, Any]) -> None:
    """Write a runtime JSON snapshot into research_data SQLite (when data_store.enabled)."""
    if not sql_store_enabled(config):
        return
    with sqlite_connection(resolve_sqlite_path(config)) as conn:
        ensure_schema(conn)
        upsert_runtime_json_artifact(conn, path, payload)


def mirror_runtime_dataframe(
    config: Dict[str, Any],
    path: str | Path,
    frame: pd.DataFrame,
    *,
    key_cols: List[str] | None = None,
) -> None:
    """Mirror a CSV-equivalent table into runtime_table_rows."""
    if not sql_store_enabled(config):
        return
    if frame is None or frame.empty:
        return
    columns = list(frame.columns)
    if not columns:
        return
    with sqlite_connection(resolve_sqlite_path(config)) as conn:
        ensure_schema(conn)
        replace_runtime_table(conn, path, frame, columns, key_cols=key_cols)


def mirror_runtime_jsonl_records(config: Dict[str, Any], path: str | Path, records: List[Dict[str, Any]]) -> None:
    """Store jsonl content as one JSON artifact so reads can prefer SQL."""
    mirror_runtime_json_artifact(
        config,
        path,
        {"_artifact_kind": "jsonl", "records": list(records or [])},
    )


def load_runtime_json_prefer_sql(
    config: Dict[str, Any],
    path: str | Path,
    default: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Prefer SQLite runtime_json_artifacts over filesystem JSON."""
    base = dict(default or {})
    p = Path(path)
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, p)
                if payload:
                    return dict(payload)
            except Exception:
                pass
    if p.exists():
        try:
            return dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return base
    return base


def load_runtime_table_any(conn: sqlite3.Connection, path: str | Path) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT payload_json
        FROM runtime_table_rows
        WHERE path_key = ?
        ORDER BY row_key
        """,
        (runtime_path_key(path),),
    ).fetchall()
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payloads.append(json.loads(_safe_text(row["payload_json"])))
        except Exception:
            continue
    return pd.DataFrame(payloads) if payloads else pd.DataFrame()


def load_runtime_dataframe_prefer_sql(config: Dict[str, Any], path: Path) -> pd.DataFrame:
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    frame = load_runtime_table_any(conn, path)
                    if not frame.empty:
                        return frame
            except Exception:
                pass
    if path.exists():
        try:
            return pd.read_csv(path)
        except Exception:
            pass
    return pd.DataFrame()


def load_runtime_jsonl_prefer_sql(config: Dict[str, Any], path: Path) -> List[Dict[str, Any]]:
    if sql_store_enabled(config):
        db_path = resolve_sqlite_path(config)
        if db_path.exists():
            try:
                with sqlite_connection(db_path) as conn:
                    payload = load_runtime_json_artifact(conn, path)
                if isinstance(payload, dict) and isinstance(payload.get("records"), list):
                    return [dict(x) for x in payload["records"]]
            except Exception:
                pass
    out: List[Dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


@contextmanager
def sqlite_connection(db_path: str | Path, *, timeout_seconds: float = 60.0) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=float(timeout_seconds))
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA busy_timeout=60000;")
        except Exception:
            pass
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS meta_kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS router_theme_registry (
            theme_id TEXT PRIMARY KEY,
            theme_name TEXT,
            theme_type TEXT,
            description TEXT,
            primary_chain TEXT,
            primary_data_sources TEXT,
            update_frequency TEXT,
            active_flag INTEGER,
            mechanism_primary TEXT,
            default_shock_type TEXT,
            key_terms TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS router_company_exposure (
            ts_code TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            theme_name TEXT,
            mechanism_primary TEXT,
            chain_position TEXT,
            exposure_strength REAL,
            benefit_direction TEXT,
            purity_score REAL,
            profit_path TEXT,
            evidence_note TEXT,
            mapping_confidence REAL,
            active_flag INTEGER,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (ts_code, theme_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS router_stock_master (
            symbol TEXT PRIMARY KEY,
            code TEXT,
            ts_code TEXT,
            name TEXT,
            industry_primary TEXT,
            industry_secondary TEXT,
            industry_bucket TEXT,
            mechanism_primary TEXT,
            subchain_primary TEXT,
            secondary_exposures TEXT,
            theme_primary TEXT,
            liquidity_bucket TEXT,
            notes TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS router_mechanism_map (
            symbol TEXT PRIMARY KEY,
            core_driver_type TEXT,
            pricing_anchor TEXT,
            benefit_mode TEXT,
            style_bucket TEXT,
            customer_anchor TEXT,
            global_vs_domestic_exposure TEXT,
            elasticity_bucket TEXT,
            defensive_vs_offensive TEXT,
            mapping_confidence REAL,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS router_blob_contracts (
            contract_name TEXT PRIMARY KEY,
            version TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS event_store_curated (
            event_id TEXT PRIMARY KEY,
            publish_date TEXT,
            publish_time TEXT,
            crawl_time TEXT,
            source_type TEXT,
            event_type TEXT,
            company_name TEXT,
            ts_code TEXT,
            importance_score REAL,
            evidence_quality_score REAL,
            anti_overfit_weight REAL,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_price_snapshot (
            trade_date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            code TEXT,
            close REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, ts_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_enriched_daily (
            code TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            name TEXT,
            adjust TEXT,
            trade_date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            amount REAL,
            pre_close REAL,
            pct_chg REAL,
            turnover_rate REAL,
            turnover_rate_f REAL,
            volume_ratio REAL,
            pe REAL,
            pb REAL,
            ps REAL,
            dv_ratio REAL,
            total_share REAL,
            float_share REAL,
            free_share REAL,
            total_mv REAL,
            circ_mv REAL,
            PRIMARY KEY (code, trade_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auxiliary_listing_master (
            ts_code TEXT PRIMARY KEY,
            code TEXT,
            name TEXT,
            industry TEXT,
            board TEXT,
            exchange TEXT,
            listed_date TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auxiliary_stock_universe (
            ts_code TEXT PRIMARY KEY,
            code TEXT,
            name TEXT,
            board TEXT,
            industry TEXT,
            is_active INTEGER,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_hs300_daily (
            trade_date TEXT PRIMARY KEY,
            close REAL,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_hs300_membership (
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            in_hs300 INTEGER,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_json_artifacts (
            path_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_jsonl_records (
            path_key TEXT NOT NULL,
            record_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (path_key, record_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_table_rows (
            path_key TEXT NOT NULL,
            row_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (path_key, row_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tender_order_raw (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetch_time TEXT NOT NULL,
            query_key TEXT,
            raw_format TEXT,
            raw_body TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            publish_date_raw TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tender_order_fact (
            id TEXT PRIMARY KEY,
            raw_record_id TEXT,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            publish_date TEXT,
            project_name TEXT,
            buyer_name TEXT,
            supplier_name TEXT,
            candidate_suppliers_json TEXT,
            amount REAL,
            amount_currency TEXT,
            equipment_type TEXT,
            province TEXT,
            city TEXT,
            theme_hint TEXT,
            stock_code_hint TEXT,
            parse_confidence REAL,
            cleaning_status TEXT,
            llm_used TEXT,
            review_required INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tender_signal_daily (
            trade_date TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            signal_score REAL,
            evidence_count INTEGER,
            evidence_ids_json TEXT,
            freshness_days REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, theme_id, stock_code, signal_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS customs_trade_raw (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetch_time TEXT NOT NULL,
            query_key TEXT,
            raw_body TEXT NOT NULL,
            raw_format TEXT,
            body_hash TEXT NOT NULL,
            period_raw TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS customs_trade_monthly (
            id TEXT PRIMARY KEY,
            raw_record_id TEXT,
            period TEXT NOT NULL,
            hs_code TEXT,
            commodity_name TEXT,
            region TEXT,
            export_amount REAL,
            export_volume REAL,
            import_amount REAL,
            import_volume REAL,
            unit TEXT,
            yoy_export_amount REAL,
            yoy_export_volume REAL,
            yoy_import_amount REAL,
            yoy_import_volume REAL,
            theme_hint TEXT,
            parse_confidence REAL,
            cleaning_status TEXT,
            llm_used TEXT,
            review_required INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS customs_signal_monthly (
            period TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            signal_score REAL,
            evidence_count INTEGER,
            evidence_ids_json TEXT,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (period, theme_id, signal_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS company_contract_raw (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetch_time TEXT NOT NULL,
            publish_time TEXT,
            title TEXT,
            raw_format TEXT,
            raw_body TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            stock_code_hint TEXT,
            company_name_hint TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS company_contract_fact (
            id TEXT PRIMARY KEY,
            raw_record_id TEXT,
            stock_code TEXT,
            company_name TEXT,
            announcement_date TEXT,
            title TEXT,
            contract_type TEXT,
            customer_name TEXT,
            contract_amount REAL,
            contract_currency TEXT,
            delivery_cycle TEXT,
            product_type TEXT,
            is_framework_agreement INTEGER,
            is_major_contract INTEGER,
            theme_hint TEXT,
            parse_confidence REAL,
            cleaning_status TEXT,
            llm_used TEXT,
            review_required INTEGER DEFAULT 0,
            source_url TEXT,
            created_at TEXT,
            updated_at TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS company_order_backlog_fact (
            id TEXT PRIMARY KEY,
            raw_record_id TEXT,
            stock_code TEXT,
            period TEXT,
            backlog_amount REAL,
            contract_liability REAL,
            prepayment REAL,
            inventory REAL,
            capex REAL,
            source_doc_type TEXT,
            parse_confidence REAL,
            cleaning_status TEXT,
            llm_used TEXT,
            review_required INTEGER DEFAULT 0,
            announcement_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS announcement_financial_fact (
            id TEXT PRIMARY KEY,
            raw_record_id TEXT,
            stock_code TEXT,
            period TEXT,
            announcement_date TEXT,
            revenue_yoy REAL,
            net_profit_yoy REAL,
            deduct_non_profit_yoy REAL,
            gross_margin REAL,
            inventory_yoy REAL,
            contract_liability_yoy REAL,
            capex_yoy REAL,
            guidance_direction TEXT,
            guidance_strength REAL,
            parse_confidence REAL,
            cleaning_status TEXT,
            llm_used TEXT,
            review_required INTEGER DEFAULT 0,
            source_url TEXT,
            created_at TEXT,
            updated_at TEXT,
            raw_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS valuation_daily (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            pe_ttm REAL,
            pb REAL,
            ps_ttm REAL,
            ev_ebitda REAL,
            pe_pct_1y REAL,
            pb_pct_1y REAL,
            ps_pct_1y REAL,
            pe_pct_industry REAL,
            pb_pct_industry REAL,
            ps_pct_industry REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS crowding_daily (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            turnover_rate REAL,
            turnover_pct_rank REAL,
            northbound_holding REAL,
            northbound_holding_change REAL,
            margin_balance REAL,
            margin_balance_change REAL,
            fund_exposure_proxy REAL,
            crowding_score REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS expectation_revision_daily (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            eps_fy1 REAL,
            eps_fy2 REAL,
            eps_revision_7d REAL,
            eps_revision_30d REAL,
            analyst_count REAL,
            revision_score REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pricing_signal_daily (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            valuation_score REAL,
            crowding_penalty REAL,
            revision_score REAL,
            underpricing_score_v2 REAL,
            pricing_state TEXT,
            pricing_confidence REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_code, theme_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS company_exposure_enriched (
            stock_code TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            exposure_strength REAL,
            purity_score REAL,
            profit_path TEXT,
            customer_concentration_hint TEXT,
            product_keywords_json TEXT,
            evidence_ids_json TEXT,
            effective_date TEXT NOT NULL,
            updated_at TEXT,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (stock_code, theme_id, effective_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS company_exposure_evidence (
            id TEXT PRIMARY KEY,
            stock_code TEXT NOT NULL,
            theme_id TEXT NOT NULL,
            source_table TEXT,
            source_record_id TEXT,
            evidence_type TEXT,
            evidence_date TEXT,
            evidence_strength REAL,
            note TEXT,
            raw_json TEXT NOT NULL
        )
        """,
    ]
    for statement in ddl:
        conn.execute(statement)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_enriched_daily_ts_code_date ON market_enriched_daily (ts_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_enriched_daily_trade_date ON market_enriched_daily (trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_jsonl_records_path_created ON runtime_jsonl_records (path_key, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_table_rows_path ON runtime_table_rows (path_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tender_order_raw_source_url ON tender_order_raw (source_name, source_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tender_order_fact_publish_stock ON tender_order_fact (publish_date, stock_code_hint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tender_signal_daily_stock_date ON tender_signal_daily (stock_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tender_signal_daily_theme_date ON tender_signal_daily (theme_id, trade_date)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_customs_trade_raw_source_url ON customs_trade_raw (source_name, source_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customs_trade_monthly_period_theme ON customs_trade_monthly (period, theme_hint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customs_signal_monthly_theme_period ON customs_signal_monthly (theme_id, period)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_company_contract_raw_source_url ON company_contract_raw (source_name, source_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_contract_fact_stock_date ON company_contract_fact (stock_code, announcement_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_order_backlog_fact_stock_period ON company_order_backlog_fact (stock_code, period)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_announcement_financial_fact_stock_period ON announcement_financial_fact (stock_code, period)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_announcement_financial_fact_date ON announcement_financial_fact (announcement_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_valuation_daily_stock_date ON valuation_daily (stock_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crowding_daily_stock_date ON crowding_daily (stock_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expectation_revision_daily_stock_date ON expectation_revision_daily (stock_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pricing_signal_daily_stock_date ON pricing_signal_daily (stock_code, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pricing_signal_daily_theme_date ON pricing_signal_daily (theme_id, trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_exposure_enriched_stock_theme ON company_exposure_enriched (stock_code, theme_id, effective_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_exposure_evidence_stock_theme ON company_exposure_evidence (stock_code, theme_id, evidence_date)")


def _replace_rows(conn: sqlite3.Connection, table: str, rows: Iterable[Dict[str, Any]]) -> int:
    payload = list(rows)
    conn.execute(f"DELETE FROM {table}")
    if not payload:
        return 0
    columns = list(payload[0].keys())
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(row.get(col) for col in columns) for row in payload])
    return len(payload)


def upsert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Dict[str, Any]],
    key_columns: List[str],
    update_columns: List[str] | None = None,
) -> int:
    payload = list(rows)
    if not payload:
        return 0
    columns = list(payload[0].keys())
    if not key_columns:
        raise ValueError("key_columns is required for upsert_rows")
    if update_columns is None:
        update_columns = [column for column in columns if column not in set(key_columns)]
    placeholders = ", ".join(["?"] * len(columns))
    if update_columns:
        updates = ", ".join([f"{column}=excluded.{column}" for column in update_columns])
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(key_columns)}) DO UPDATE SET {updates}"
        )
    else:
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(key_columns)}) DO NOTHING"
        )
    conn.executemany(sql, [tuple(row.get(col) for col in columns) for row in payload])
    return len(payload)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def import_frame(conn: sqlite3.Connection, table: str, frame: pd.DataFrame, columns: List[str]) -> int:
    if frame.empty:
        conn.execute(f"DELETE FROM {table}")
        return 0
    rows: List[Dict[str, Any]] = []
    for _, row in frame.fillna("").iterrows():
        item = row.to_dict()
        payload = {column: item.get(column, "") for column in columns}
        payload["raw_json"] = json.dumps(item, ensure_ascii=False, sort_keys=True)
        rows.append(payload)
    return _replace_rows(conn=conn, table=table, rows=rows)


def import_blob_contract(conn: sqlite3.Connection, contract_name: str, payload: Dict[str, Any], version: str = "") -> int:
    conn.execute(
        """
        INSERT INTO router_blob_contracts (contract_name, version, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(contract_name) DO UPDATE SET
            version=excluded.version,
            payload_json=excluded.payload_json,
            updated_at=CURRENT_TIMESTAMP
        """,
        (contract_name, version, json.dumps(payload, ensure_ascii=False, indent=2)),
    )
    return 1


def import_event_store_jsonl(conn: sqlite3.Connection, path: Path) -> int:
    rows: List[Dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except Exception:
                    continue
                event_id = _safe_text(item.get("event_id")) or f"event_{len(rows) + 1}"
                rows.append(
                    {
                        "event_id": event_id,
                        "publish_date": _safe_text(item.get("publish_time") or item.get("crawl_time") or item.get("date"))[:10],
                        "publish_time": _safe_text(item.get("publish_time")),
                        "crawl_time": _safe_text(item.get("crawl_time")),
                        "source_type": _safe_text(item.get("source_type")),
                        "event_type": _safe_text(item.get("event_type")),
                        "company_name": _safe_text(item.get("company_name") or item.get("company_name_hint")),
                        "ts_code": _safe_text(item.get("ts_code") or item.get("symbol") or item.get("security_code")),
                        "importance_score": item.get("importance_score", item.get("importance", "")),
                        "evidence_quality_score": item.get("evidence_quality_score", ""),
                        "anti_overfit_weight": item.get("anti_overfit_weight", ""),
                        "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                    }
                )
    return _replace_rows(conn=conn, table="event_store_curated", rows=rows)


def import_price_snapshot_csv(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        conn.execute("DELETE FROM market_price_snapshot")
        return 0
    frame = pd.read_csv(path).fillna("")
    rows: List[Dict[str, Any]] = []
    for _, row in frame.iterrows():
        item = row.to_dict()
        ts_code = _safe_text(item.get("ts_code"))
        if not ts_code:
            continue
        rows.append(
            {
                "trade_date": _safe_text(item.get("date"))[:10],
                "ts_code": ts_code,
                "code": _safe_text(item.get("code")),
                "close": item.get("close", ""),
                "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
        )
    return _replace_rows(conn=conn, table="market_price_snapshot", rows=rows)


def _normalize_code(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _normalize_ts_code(value: Any) -> str:
    text = _safe_text(value).upper()
    if not text:
        return ""
    if "." in text:
        return text
    code = _normalize_code(text)
    if not code:
        return ""
    if code.startswith(("600", "601", "603", "605", "688", "900")):
        return f"{code}.SH"
    if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return f"{code}.SZ"
    if code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "876", "877", "878", "879", "880", "881", "882", "883", "884", "885", "886", "887", "888", "889", "920")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def import_enriched_daily_dir(conn: sqlite3.Connection, enriched_dir: Path) -> int:
    conn.execute("DELETE FROM market_enriched_daily")
    if not enriched_dir.exists():
        return 0
    columns = [
        "code",
        "ts_code",
        "name",
        "adjust",
        "trade_date",
        "open",
        "close",
        "high",
        "low",
        "amount",
        "pre_close",
        "pct_chg",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pb",
        "ps",
        "dv_ratio",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ]
    insert_sql = f"INSERT INTO market_enriched_daily ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})"
    inserted = 0
    for file_path in sorted(enriched_dir.glob("*.csv")):
        try:
            frame = pd.read_csv(file_path, encoding="utf-8-sig").fillna("")
        except Exception:
            continue
        if frame.empty:
            continue
        rows = []
        for _, row in frame.iterrows():
            item = row.to_dict()
            code = _normalize_code(item.get("code") or file_path.stem)
            trade_date = _safe_text(item.get("date"))[:10]
            if not code or not trade_date:
                continue
            rows.append(
                (
                    code,
                    _normalize_ts_code(code),
                    _safe_text(item.get("name")),
                    _safe_text(item.get("adjust")),
                    trade_date,
                    item.get("open", ""),
                    item.get("close", ""),
                    item.get("high", ""),
                    item.get("low", ""),
                    item.get("amount", ""),
                    item.get("pre_close", ""),
                    item.get("pct_chg", ""),
                    item.get("turnover_rate", ""),
                    item.get("turnover_rate_f", ""),
                    item.get("volume_ratio", ""),
                    item.get("pe", ""),
                    item.get("pb", ""),
                    item.get("ps", ""),
                    item.get("dv_ratio", ""),
                    item.get("total_share", ""),
                    item.get("float_share", ""),
                    item.get("free_share", ""),
                    item.get("total_mv", ""),
                    item.get("circ_mv", ""),
                )
            )
        if not rows:
            continue
        conn.executemany(insert_sql, rows)
        inserted += len(rows)
    return inserted


def fetch_enriched_history(
    conn: sqlite3.Connection,
    code: str,
    limit: int | None = None,
    start_date: str = "",
    end_date: str = "",
    columns: List[str] | None = None,
) -> pd.DataFrame:
    selected = columns or [
        "trade_date AS date",
        "code",
        "ts_code",
        "name",
        "adjust",
        "open",
        "close",
        "high",
        "low",
        "amount",
        "pre_close",
        "pct_chg",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pb",
        "ps",
        "dv_ratio",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ]
    where = ["code = ?"]
    params: List[Any] = [_normalize_code(code)]
    if start_date:
        where.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("trade_date <= ?")
        params.append(end_date)
    sql = f"SELECT {', '.join(selected)} FROM market_enriched_daily WHERE {' AND '.join(where)} ORDER BY trade_date DESC"
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    frame = fetch_frame(conn, sql, params)
    if not frame.empty and "date" in frame.columns:
        frame = frame.sort_values("date").reset_index(drop=True)
    return frame


def import_generic_csv(
    conn: sqlite3.Connection,
    path: Path,
    table: str,
    column_map: Dict[str, str],
    key_field: str,
) -> int:
    if not path.exists():
        conn.execute(f"DELETE FROM {table}")
        return 0
    frame = pd.read_csv(path, encoding="utf-8-sig").fillna("")
    rows: List[Dict[str, Any]] = []
    for _, row in frame.iterrows():
        item = row.to_dict()
        payload = {target: item.get(source, "") for source, target in column_map.items()}
        if not _safe_text(payload.get(key_field)):
            continue
        payload["raw_json"] = json.dumps(item, ensure_ascii=False, sort_keys=True)
        rows.append(payload)
    return _replace_rows(conn=conn, table=table, rows=rows)


def fetch_frame(conn: sqlite3.Connection, sql: str, params: Iterable[Any] | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=list(params or []))


def fetch_blob_contract(conn: sqlite3.Connection, contract_name: str) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM router_blob_contracts WHERE contract_name = ?",
        (contract_name,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(_safe_text(row["payload_json"]))
    except Exception:
        return None


def fetch_json_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] | None = None) -> List[Dict[str, Any]]:
    rows = conn.execute(sql, tuple(params or [])).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        raw_json = _safe_text(row["raw_json"]) if "raw_json" in row.keys() else ""
        if raw_json:
            try:
                items.append(json.loads(raw_json))
                continue
            except Exception:
                pass
        items.append(dict(row))
    return items


def runtime_path_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def upsert_runtime_json_artifact(conn: sqlite3.Connection, path: str | Path, payload: Dict[str, Any]) -> str:
    path_key = runtime_path_key(path)
    conn.execute(
        """
        INSERT INTO runtime_json_artifacts (path_key, payload_json, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(path_key) DO UPDATE SET
            payload_json=excluded.payload_json,
            updated_at=CURRENT_TIMESTAMP
        """,
        (path_key, json.dumps(payload, ensure_ascii=False, default=str)),
    )
    return path_key


def load_runtime_json_artifact(conn: sqlite3.Connection, path: str | Path) -> Dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM runtime_json_artifacts WHERE path_key = ?",
        (runtime_path_key(path),),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(_safe_text(row["payload_json"]))
    except Exception:
        return None


def append_runtime_jsonl_record(conn: sqlite3.Connection, path: str | Path, payload: Dict[str, Any], record_id: str = "") -> str:
    path_key = runtime_path_key(path)
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    stable_id = _safe_text(record_id) or __import__("hashlib").sha1(payload_json.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT OR REPLACE INTO runtime_jsonl_records (path_key, record_id, payload_json, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (path_key, stable_id, payload_json),
    )
    return stable_id


def load_runtime_jsonl_records(conn: sqlite3.Connection, path: str | Path) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT payload_json
        FROM runtime_jsonl_records
        WHERE path_key = ?
        ORDER BY created_at, record_id
        """,
        (runtime_path_key(path),),
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        try:
            items.append(json.loads(_safe_text(row["payload_json"])))
        except Exception:
            continue
    return items


def replace_runtime_table(
    conn: sqlite3.Connection,
    path: str | Path,
    frame: pd.DataFrame,
    columns: List[str],
    key_cols: List[str] | None = None,
) -> int:
    path_key = runtime_path_key(path)
    conn.execute("DELETE FROM runtime_table_rows WHERE path_key = ?", (path_key,))
    out = frame.copy() if frame is not None else pd.DataFrame(columns=columns)
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[columns].copy()
    if key_cols:
        out = out.drop_duplicates(subset=key_cols, keep="last")
    rows = []
    for idx, row in out.iterrows():
        item = row.where(pd.notna(row), None).to_dict()
        if key_cols:
            row_key = "|".join(_safe_text(item.get(col)) for col in key_cols)
        else:
            row_key = str(idx)
        rows.append((path_key, row_key, json.dumps(item, ensure_ascii=False, default=str)))
    if rows:
        conn.executemany(
            """
            INSERT INTO runtime_table_rows (path_key, row_key, payload_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            rows,
        )
    return len(rows)


def load_runtime_table(conn: sqlite3.Connection, path: str | Path, columns: List[str]) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT payload_json
        FROM runtime_table_rows
        WHERE path_key = ?
        ORDER BY row_key
        """,
        (runtime_path_key(path),),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=columns)
    payloads = []
    for row in rows:
        try:
            payloads.append(json.loads(_safe_text(row["payload_json"])))
        except Exception:
            continue
    if not payloads:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(payloads)
    for col in columns:
        if col not in frame.columns:
            frame[col] = pd.NA
    return frame[columns].copy()
