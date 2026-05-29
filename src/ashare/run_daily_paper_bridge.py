from __future__ import annotations

import argparse
import json
from pathlib import Path

from live_execution_bridge.runtime import run_once


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Args:
        无。

    Returns:
        argparse.Namespace: 解析结果。
    """
    parser = argparse.ArgumentParser(description="日频模拟执行桥")
    parser.add_argument("--config", required=True, help="配置文件路径")
    return parser.parse_args()


def main() -> None:
    """主函数。

    Args:
        无。

    Returns:
        None: 无返回值。
    """
    args = parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = run_once(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
