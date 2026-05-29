from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.oms.tactical_merge import merge_tactical_orders_into_control_result
from live_execution_bridge.models import AccountState, OrderIntent, Position


def _acct(shares: int = 1000, last_px: float = 10.0) -> AccountState:
    return AccountState(
        account_id="t",
        cash=1e6,
        nav_value=2e6,
        positions=[Position(symbol="600000.SH", shares=shares, avg_cost=9.0, last_price=last_px, available_shares=shares)],
    )


class TacticalMergeTest(unittest.TestCase):
    def test_missing_file_noop(self) -> None:
        ctrl = {"final_orders": []}
        out, audit = merge_tactical_orders_into_control_result(
            ctrl,
            Path("/nonexistent/path/intraday_tactical_orders.json"),
            _acct(),
            {},
        )
        self.assertEqual(out["final_orders"], [])
        self.assertFalse(audit["applied"])

    def test_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            p.write_text("{not json", encoding="utf-8")
            ctrl = {"final_orders": []}
            _, audit = merge_tactical_orders_into_control_result(ctrl, p, _acct(), {})
            self.assertEqual(audit.get("error"), "read_failed")

    def test_single_buy_merges_target_and_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            p.write_text(
                json.dumps(
                    {
                        "orders": [
                            {
                                "symbol": "600000.SH",
                                "side": "BUY",
                                "delta_shares": 200,
                                "ref_price": 10.5,
                                "reason_code": "tp_soft",
                                "intent_class": "reduce_risk",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ctrl: dict = {"final_orders": []}
            out, audit = merge_tactical_orders_into_control_result(ctrl, p, _acct(shares=1000), {})
            self.assertTrue(audit["applied"])
            fo = out["final_orders"]
            self.assertEqual(len(fo), 1)
            o = fo[0]
            self.assertEqual(o.delta_shares, 200)
            self.assertEqual(o.target_shares, 1200)
            self.assertEqual(o.side, "BUY")

    def test_same_key_aggregates_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            p.write_text(
                json.dumps(
                    {
                        "orders": [
                            {"symbol": "600000.SH", "side": "BUY", "delta_shares": 100, "ref_price": 10.0, "reason_code": "a", "intent_class": "x"},
                            {"symbol": "600000.SH", "side": "BUY", "delta_shares": 300, "ref_price": 10.2, "reason_code": "b", "intent_class": "y"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            base = OrderIntent(symbol="600000.SH", side="BUY", target_shares=500, delta_shares=50, ref_price=9.9, reason="base")
            ctrl = {"final_orders": [base]}
            out, _ = merge_tactical_orders_into_control_result(ctrl, p, _acct(shares=1000), {})
            fo = out["final_orders"]
            self.assertEqual(len(fo), 1)
            o = fo[0]
            self.assertEqual(o.delta_shares, 450)
            # Position 1000 + tactical rows: max(target from each row) vs cumulative delta
            self.assertEqual(o.target_shares, 1300)

    def test_ref_price_from_price_map_when_row_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            p.write_text(
                json.dumps(
                    {
                        "orders": [
                            {"symbol": "600000.SH", "side": "SELL", "delta_shares": 100, "ref_price": 0, "reason_code": "x", "intent_class": "y"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ctrl = {"final_orders": []}
            out, _ = merge_tactical_orders_into_control_result(
                ctrl,
                p,
                _acct(shares=1000, last_px=11.0),
                {"600000.SH": 12.34},
            )
            o = out["final_orders"][0]
            self.assertEqual(o.ref_price, 12.34)
            self.assertEqual(o.target_shares, 900)

    def test_skips_bad_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            p.write_text(
                json.dumps(
                    {
                        "orders": [
                            {"symbol": "", "side": "BUY", "delta_shares": 100, "ref_price": 10},
                            {"symbol": "600000.SH", "side": "HOLD", "delta_shares": 100, "ref_price": 10},
                            {"symbol": "600000.SH", "side": "BUY", "delta_shares": 0, "ref_price": 10},
                            {"symbol": "600000.SH", "side": "BUY", "delta_shares": 50, "ref_price": 10, "reason_code": "ok", "intent_class": "z"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ctrl = {"final_orders": []}
            out, audit = merge_tactical_orders_into_control_result(ctrl, p, _acct(), {})
            self.assertEqual(len(out["final_orders"]), 1)
            self.assertEqual(out["final_orders"][0].delta_shares, 50)
            self.assertEqual(audit["n_tactical"], 4)


if __name__ == "__main__":
    unittest.main()
