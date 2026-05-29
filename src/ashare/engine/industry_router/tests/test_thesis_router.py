from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from engine.industry_router import build_industry_router_artifacts
from engine.industry_router.core.loaders import (
    load_company_exposure_map,
    load_theme_registry,
    resolve_stock_master,
)
from engine.industry_router.registry import build_theme_registry_runtime
from engine.market_state.core.feature_builder import build_market_feature_snapshot
from engine.portfolio_recommendation import _load_router_signal_context


class ThesisRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.contract_root = Path(__file__).resolve().parents[3] / "configs" / "industry_router"
        self.research_root = self.root / "research"
        self.output_root = self.research_root / "industry_router"
        self.market_state_root = self.root / "market_state"
        self.event_store_root = self.root / "event_store"
        self.enriched_dir = self.root / "enriched"
        self.enriched_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.root / "price_snapshot.csv"
        self.hs300_path = self.root / "hs300.csv"
        self.stock_master_df = pd.read_csv(self.contract_root / "stock_master.seed.csv", encoding="utf-8-sig")
        self._write_market_inputs()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _base_config(self, with_market_inputs: bool = True) -> dict:
        empty_enriched = self.root / "empty_enriched"
        empty_enriched.mkdir(exist_ok=True)
        return {
            "industry_router": {
                "enabled": True,
                "contract_root": str(self.contract_root),
                "output_root": str(self.output_root),
                "source_fetch": {
                    "enabled": False,
                },
            },
            "paths": {
                "research_root": str(self.research_root),
                "event_store_root": str(self.event_store_root),
                "industry_router_output_root": str(self.output_root),
                "market_state_root": str(self.market_state_root),
                "log_root": str(self.root / "logs"),
            },
            "market_pipeline": {
                "price_snapshot_path": str(self.snapshot_path if with_market_inputs else self.root / "missing_snapshot.csv"),
                "enriched_dir": str(self.enriched_dir if with_market_inputs else empty_enriched),
                "hs300_path": str(self.hs300_path if with_market_inputs else self.root / "missing_hs300.csv"),
            },
        }

    def _write_market_inputs(self) -> None:
        snapshot_rows = []
        dates = pd.date_range("2026-03-01", periods=30, freq="D")
        for idx, row in self.stock_master_df.iterrows():
            ts_code = str(row["symbol"]).strip().upper()
            code = ts_code.split(".", 1)[0]
            base_price = 8.0 + idx * 2.7
            closes = []
            for day, date in enumerate(dates):
                drift = 0.004 * day
                if ts_code in {"300308.SZ", "002463.SZ"} and day >= 24:
                    drift -= 0.018 * (day - 23)
                if ts_code in {"600036.SH", "601398.SH", "601318.SH"}:
                    drift = 0.0015 * day
                price = round(base_price * (1.0 + drift), 4)
                closes.append(price)
            price_frame = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": closes, "amount": [250000 + idx * 1000] * len(dates)})
            price_frame.to_csv(self.enriched_dir / f"{code}.csv", index=False)
            last_close = closes[-1]
            prev_close = closes[-2]
            snapshot_rows.append(
                {
                    "date": dates[-1].strftime("%Y-%m-%d"),
                    "code": code,
                    "ts_code": ts_code,
                    "close": last_close,
                    "pre_close": prev_close,
                    "pct_chg": round((last_close / prev_close - 1.0) * 100.0, 4),
                    "amount": 300000 + idx * 2000,
                    "turnover_rate": 2.5 + idx * 0.03,
                    "total_mv": 5000000 + idx * 50000,
                    "circ_mv": 3000000 + idx * 30000,
                }
            )
        pd.DataFrame(snapshot_rows).to_csv(self.snapshot_path, index=False)
        hs300 = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "close": [3800 + day * 4 for day in range(len(dates))],
            }
        )
        hs300.to_csv(self.hs300_path, index=False)

    @staticmethod
    def _events() -> list[dict]:
        return [
            {
                "event_id": "evt_ai_chain_1",
                "publish_time": "2026-03-27 08:30:00",
                "source_type": "announcement",
                "event_type": "major_contract",
                "security_code": "300308.SZ",
                "company_name": "中际旭创",
                "raw_title": "中际旭创公告 800G 光模块订单继续增长",
                "summary": "公司披露 800G 光模块订单景气延续，AI 光互连需求继续提升。",
                "event_direction": "positive",
                "importance_score": 0.82,
                "evidence_quality_score": 0.84,
                "anti_overfit_weight": 0.92,
            },
            {
                "event_id": "evt_broker_1",
                "publish_time": "2026-03-27 09:10:00",
                "source_type": "news",
                "event_type": "policy_industry_event",
                "security_code": "601688.SH",
                "company_name": "华泰证券",
                "raw_title": "成交额回升 券商板块弹性提升",
                "summary": "风险偏好改善带动成交额回暖，券商受益于资本市场活跃度上行。",
                "event_direction": "positive",
                "importance_score": 0.70,
                "evidence_quality_score": 0.68,
                "anti_overfit_weight": 0.88,
            },
        ]

    def test_registry_contracts_build_runtime_theme_objects(self) -> None:
        config = self._base_config()
        theme_df = load_theme_registry(self.contract_root)
        exposure_df = load_company_exposure_map(self.contract_root)
        stock_master_df = resolve_stock_master(config=config, contract_root_path=self.contract_root)
        registry = build_theme_registry_runtime(theme_registry_df=theme_df, exposure_df=exposure_df, stock_master_df=stock_master_df)
        self.assertEqual(len(registry.themes), 10)
        self.assertEqual(len(registry.exposures), 14)
        self.assertEqual(len(registry.exposures_by_theme["ai_compute_chain"]), 2)

    def test_event_only_inputs_do_not_authorize_entry(self) -> None:
        config = self._base_config(with_market_inputs=False)
        result = build_industry_router_artifacts(config=config, structured_events=self._events())
        latest = pd.read_csv(result["latest_signal_path"])
        self.assertFalse(latest["allow_entry"].astype(bool).any())
        summary = result["summary"]
        self.assertEqual(summary["active_thesis_count"], 0)

    def test_router_outputs_required_thesis_contracts(self) -> None:
        config = self._base_config()
        result = build_industry_router_artifacts(config=config, structured_events=self._events())
        latest = pd.read_csv(result["latest_signal_path"])
        required = {"ts_code", "theme_id", "thesis_id", "shock_type", "evidence_score", "final_score", "allow_entry", "signal_state"}
        self.assertTrue(required.issubset(set(latest.columns)))
        summary = result["summary"]
        self.assertIn("active_theses", summary)
        self.assertIn("context_payload", summary)
        self.assertTrue((self.output_root / "thesis_daily.csv").exists())

    def test_score_card_fields_present_in_latest_signal(self) -> None:
        config = self._base_config()
        result = build_industry_router_artifacts(config=config, structured_events=self._events())
        latest = pd.read_csv(result["latest_signal_path"])
        for column in ["evidence_score", "persistence_score", "underpricing_score", "crowding_penalty", "final_score"]:
            self.assertIn(column, latest.columns)
            self.assertTrue(pd.to_numeric(latest[column], errors="coerce").notna().all())
        self.assertTrue(latest["theme_id"].astype(str).str.len().gt(0).any())
        self.assertTrue(latest["thesis_id"].astype(str).str.len().gt(0).any())

    def test_market_state_feature_builder_reads_thesis_mechanism_scores(self) -> None:
        config = self._base_config()
        build_industry_router_artifacts(config=config, structured_events=self._events())
        feature_snapshot = build_market_feature_snapshot(config=config, output_root=self.market_state_root)
        self.assertTrue(feature_snapshot["ok"])
        self.assertTrue(feature_snapshot["mechanism_scores"])
        self.assertTrue(set(feature_snapshot["mechanism_scores"].keys()).issubset({"trend_capex", "price_inventory", "macro_style"}))

    def test_portfolio_context_reads_thesis_signal_contract(self) -> None:
        config = self._base_config()
        build_industry_router_artifacts(config=config, structured_events=self._events())
        router_df = _load_router_signal_context(config=config)
        self.assertIn("theme_id", router_df.columns)
        self.assertIn("thesis_id", router_df.columns)
        self.assertTrue((router_df["router_final_score"] >= 0).all())


if __name__ == "__main__":
    unittest.main()
