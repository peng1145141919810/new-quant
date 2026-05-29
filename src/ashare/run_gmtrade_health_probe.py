from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path

from live_execution_bridge.health_probe import probe_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行掘金账户健康探针")
    parser.add_argument("--config", required=True, help="运行配置文件路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        report = probe_once(config)
    except Exception as exc:
        report = {
            "ok": False,
            "error": str(exc),
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
