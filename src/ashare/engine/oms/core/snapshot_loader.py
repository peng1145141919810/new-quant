from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from live_execution_bridge.brokers.gmtrade_sim_broker import GMTradeSimBroker
from live_execution_bridge.io_portfolio import build_price_map, load_target_positions
from live_execution_bridge.utils import safe_float


def build_broker(config: Dict[str, Any]) -> GMTradeSimBroker:
    broker_cfg = dict(config.get("broker", {}) or {})
    return GMTradeSimBroker(
        token=str(broker_cfg["token"]),
        account_id=str(broker_cfg["account_id"]),
        account_alias=str(broker_cfg.get("account_alias", "")),
        endpoint=str(broker_cfg.get("endpoint", "api.myquant.cn:9000")),
        buy_price_ratio=float(broker_cfg.get("buy_price_ratio", 1.01)),
        sell_price_ratio=float(broker_cfg.get("sell_price_ratio", 0.99)),
        order_wait_seconds=float(broker_cfg.get("order_wait_seconds", 3.0)),
        sell_by_available=bool(broker_cfg.get("sell_by_available", True)),
    )


def load_execution_snapshots(config: Dict[str, Any]) -> Dict[str, Any]:
    portfolio_path, portfolio_frame, target_positions = load_target_positions(config)
    price_map = build_price_map(portfolio_frame, config.get("price_snapshot_path", ""))
    broker = build_broker(config)
    account_state = broker.load_account_state()
    for pos in account_state.positions:
        price_map.setdefault(pos.symbol, safe_float(pos.last_price, 0.0))
    order_health = broker.load_order_health()
    fill_rows = broker.load_fill_rows()
    return {
        "broker": broker,
        "portfolio_path": portfolio_path,
        "portfolio_frame": portfolio_frame,
        "target_positions": target_positions,
        "price_map": price_map,
        "account_state": account_state,
        "order_health": order_health,
        "fill_rows": fill_rows,
    }
