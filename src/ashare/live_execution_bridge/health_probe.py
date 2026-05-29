from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from engine.oms.core.snapshot_loader import build_broker


def probe_once(config: Dict[str, Any]) -> Dict[str, Any]:
    """读取账户、持仓与委托真相，不触发任何交易动作。"""
    broker = build_broker(config)
    account_state = broker.load_account_state()
    order_health = broker.load_order_health()
    broker_cfg = dict(config.get("broker", {}) or {})
    execution_policy = dict(config.get("execution_policy", {}) or {})
    return {
        "ok": True,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "broker_type": "gmtrade_sim",
        "execution_policy": execution_policy,
        "broker": {
            "account_id": str(account_state.account_id or broker_cfg.get("account_id", "") or ""),
            "account_alias": str(broker_cfg.get("account_alias", "") or ""),
            "selected_account_mode": str(broker_cfg.get("selected_account_mode", "") or ""),
        },
        "account_state": account_state.to_dict(),
        "positions_count": len(account_state.positions),
        "order_health": order_health,
    }
