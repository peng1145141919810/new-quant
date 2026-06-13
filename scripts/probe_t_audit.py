from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "src" / "ashare"
if str(HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(HUB_ROOT))

from engine.config_utils import load_config  # noqa: E402
from engine.portfolio_release import load_latest_release  # noqa: E402
from engine.strategy_audit import build_strategy_audit_pack  # noqa: E402
from engine.t_audit import build_t_audit_pack  # noqa: E402


def _fallback_release_doc(config: dict) -> dict:
    intraday_root = Path(str(config.get("paths", {}).get("trade_clock_root", REPO_ROOT / "data" / "trade_clock") or REPO_ROOT / "data" / "trade_clock")) / "intraday_state" / "latest"
    control = {}
    try:
        control = json.loads((intraday_root / "intraday_control_summary.json").read_text(encoding="utf-8"))
    except Exception:
        control = {}
    trade_date = str(control.get("trade_date", "") or "1970-01-01")
    release_id = str(control.get("release_id", "") or "synthetic_t_audit_probe")
    return {
        "trade_date": trade_date,
        "release_id": release_id,
        "artifacts": {},
    }


def main() -> int:
    config_path = HUB_ROOT / "configs" / "hub_config.runtime.daily_production.json"
    config = load_config(config_path)
    try:
        release_doc = load_latest_release(config=config)
    except Exception:
        release_doc = _fallback_release_doc(config)
    trade_date = str(release_doc.get("trade_date", "") or "")
    if not trade_date:
        trade_date = "1970-01-01"
    pack_dir = REPO_ROOT / "tmp" / "t_audit_probe"
    payload = build_t_audit_pack(config=config, trade_date=trade_date, release_doc=release_doc, pack_dir=pack_dir)
    audit = build_strategy_audit_pack(config=config, trade_date=trade_date, release_doc=release_doc, pack_dir=pack_dir / "strategy")
    result = {
        "trade_date": trade_date,
        "release_id": str(release_doc.get("release_id", "") or ""),
        "t_audit_available": bool(payload.get("available", False)),
        "t_top_reject_reason": str(payload.get("top_reject_reason", "") or ""),
        "t_top_suited_mechanism": str(payload.get("top_suited_mechanism", "") or ""),
        "strategy_audit_json": audit.get("json_path", ""),
        "strategy_audit_html": audit.get("html_path", ""),
        "t_audit_json": str(pack_dir / "t_audit.json"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
