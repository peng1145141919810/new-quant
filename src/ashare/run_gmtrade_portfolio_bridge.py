from __future__ import annotations

import argparse
import json
from pathlib import Path

from live_execution_bridge.runtime import run_once


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Args:
        None

    Returns:
        argparse.Namespace: 参数对象。
    """
    parser = argparse.ArgumentParser(description="执行掘金仿真调仓桥接脚本")
    parser.add_argument("--config", required=True, help="运行配置文件路径")
    return parser.parse_args()


def main() -> None:
    """主入口。

    Args:
        None

    Returns:
        None
    """
    args = parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    report = run_once(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
