from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


def _bootstrap_repo() -> None:
    script_path = Path(__file__).resolve()
    package_root = script_path.parents[1] / "src" / "ashare"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))


_bootstrap_repo()

from engine.config_builder import build_runtime_config
from engine.sql_store import (
    append_runtime_jsonl_record,
    ensure_schema,
    import_blob_contract,
    import_enriched_daily_dir,
    import_event_store_jsonl,
    import_frame,
    import_generic_csv,
    import_price_snapshot_csv,
    replace_runtime_table,
    resolve_sqlite_path,
    sqlite_connection,
    upsert_runtime_json_artifact,
)


def _import_runtime_tree(conn, root: Path) -> dict:
    summary = {"json": 0, "jsonl": 0, "csv": 0}
    if not root.exists():
        return summary
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                upsert_runtime_json_artifact(conn, path, json.loads(path.read_text(encoding="utf-8")))
                summary["json"] += 1
            elif suffix == ".jsonl":
                lines = path.read_text(encoding="utf-8").splitlines()
                for idx, line in enumerate(lines, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    append_runtime_jsonl_record(conn, path, json.loads(text), record_id=f"{idx:09d}")
                summary["jsonl"] += 1
            elif suffix == ".csv":
                frame = pd.read_csv(path, encoding="utf-8-sig").fillna("")
                replace_runtime_table(conn, path, frame, list(frame.columns), key_cols=None)
                summary["csv"] += 1
        except Exception:
            continue
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the SQLite research store from current CSV/JSONL sources.")
    parser.add_argument("--db-path", default="", help="Override sqlite target path.")
    args = parser.parse_args()

    config = build_runtime_config()
    db_path = Path(args.db_path).resolve() if args.db_path else resolve_sqlite_path(config)
    contract_root = Path(str(config["industry_router"]["contract_root"]))
    paths = dict(config.get("paths", {}) or {})
    market_cfg = dict(config.get("market_pipeline", {}) or {})

    with sqlite_connection(db_path) as conn:
        ensure_schema(conn)
        summary = {"db_path": str(db_path), "imports": {}}

        summary["imports"]["router_theme_registry"] = import_frame(
            conn,
            "router_theme_registry",
            pd.read_csv(contract_root / "theme_registry.seed.csv", encoding="utf-8-sig").fillna(""),
            [
                "theme_id",
                "theme_name",
                "theme_type",
                "description",
                "primary_chain",
                "primary_data_sources",
                "update_frequency",
                "active_flag",
                "mechanism_primary",
                "default_shock_type",
                "key_terms",
            ],
        )
        summary["imports"]["router_company_exposure"] = import_frame(
            conn,
            "router_company_exposure",
            pd.read_csv(contract_root / "company_exposure_map.seed.csv", encoding="utf-8-sig").fillna(""),
            [
                "ts_code",
                "theme_id",
                "theme_name",
                "mechanism_primary",
                "chain_position",
                "exposure_strength",
                "benefit_direction",
                "purity_score",
                "profit_path",
                "evidence_note",
                "mapping_confidence",
                "active_flag",
            ],
        )
        summary["imports"]["router_stock_master"] = import_frame(
            conn,
            "router_stock_master",
            pd.read_csv(contract_root / "stock_master.seed.csv", encoding="utf-8-sig").fillna(""),
            [
                "symbol",
                "code",
                "ts_code",
                "name",
                "industry_primary",
                "industry_secondary",
                "industry_bucket",
                "mechanism_primary",
                "subchain_primary",
                "secondary_exposures",
                "theme_primary",
                "liquidity_bucket",
                "notes",
            ],
        )
        summary["imports"]["router_mechanism_map"] = import_frame(
            conn,
            "router_mechanism_map",
            pd.read_csv(contract_root / "mechanism_map.seed.csv", encoding="utf-8-sig").fillna(""),
            [
                "symbol",
                "core_driver_type",
                "pricing_anchor",
                "benefit_mode",
                "style_bucket",
                "customer_anchor",
                "global_vs_domestic_exposure",
                "elasticity_bucket",
                "defensive_vs_offensive",
                "mapping_confidence",
            ],
        )

        for name in ["strategy_spec", "source_contracts", "event_taxonomy"]:
            payload = json.loads((contract_root / f"{name}.json").read_text(encoding="utf-8-sig"))
            summary["imports"][f"router_blob_contracts.{name}"] = import_blob_contract(
                conn=conn,
                contract_name=name,
                payload=payload,
                version=str(payload.get("spec_version") or payload.get("contract_version") or ""),
            )

        summary["imports"]["event_store_curated"] = import_event_store_jsonl(
            conn=conn,
            path=Path(str(paths.get("event_store_root", ""))) / "event_store.jsonl",
        )
        summary["imports"]["market_price_snapshot"] = import_price_snapshot_csv(
            conn=conn,
            path=Path(str(market_cfg.get("price_snapshot_path", ""))),
        )
        summary["imports"]["market_enriched_daily"] = import_enriched_daily_dir(
            conn=conn,
            enriched_dir=Path(str(market_cfg.get("enriched_dir", ""))),
        )
        summary["imports"]["auxiliary_listing_master"] = import_generic_csv(
            conn=conn,
            path=Path(str(market_cfg.get("listing_master_path", ""))),
            table="auxiliary_listing_master",
            column_map={
                "ts_code": "ts_code",
                "code": "code",
                "name": "name",
                "industry": "industry",
                "board": "board",
                "exchange": "exchange",
                "list_date": "listed_date",
            },
            key_field="ts_code",
        )
        summary["imports"]["auxiliary_stock_universe"] = import_generic_csv(
            conn=conn,
            path=Path(str(market_cfg.get("stock_universe_path", ""))),
            table="auxiliary_stock_universe",
            column_map={
                "ts_code": "ts_code",
                "code": "code",
                "name": "name",
                "board": "board",
                "industry": "industry",
                "is_active": "is_active",
            },
            key_field="ts_code",
        )
        summary["imports"]["market_hs300_daily"] = import_generic_csv(
            conn=conn,
            path=Path(str(market_cfg.get("hs300_path", ""))),
            table="market_hs300_daily",
            column_map={"date": "trade_date", "close": "close"},
            key_field="trade_date",
        )
        summary["imports"]["market_hs300_membership"] = import_generic_csv(
            conn=conn,
            path=Path(str(market_cfg.get("hs300_membership_history_path", ""))),
            table="market_hs300_membership",
            column_map={"date": "trade_date", "code": "code", "in_hs300": "in_hs300"},
            key_field="trade_date",
        )
        summary["imports"]["runtime_trade_release"] = _import_runtime_tree(conn, Path(str(paths.get("trade_release_root", ""))))
        summary["imports"]["runtime_trade_clock"] = _import_runtime_tree(conn, Path(str(paths.get("trade_clock_root", ""))))
        summary["imports"]["runtime_oms"] = _import_runtime_tree(conn, Path(str(paths.get("oms_output_root", ""))))

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
