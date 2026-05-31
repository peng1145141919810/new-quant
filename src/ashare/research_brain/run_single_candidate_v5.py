#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hub.single_run_v5 import execute_single_experiment_v5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run one V5 candidate config in an isolated process')
    parser.add_argument('--config', required=True)
    parser.add_argument('--result-path', required=True)
    parser.add_argument('--dry-run', dest='dry_run', action='store_true')
    parser.add_argument('--no-dry-run', dest='dry_run', action='store_false')
    parser.set_defaults(dry_run=False)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    result_path = Path(args.result_path)
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    result = execute_single_experiment_v5(config=config, dry_run=bool(args.dry_run))
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    # 关键：用 os._exit(0) 跳过 Python 自然 shutdown 阶段。
    #
    # 原因：lightgbm_gpu (有时也包括 xgboost_gpu) 在 Python 解释器 shutdown 阶段
    # 触发 GPU 资源清理，在某些 LightGBM × CUDA 组合上会原生崩溃，进程 exit 120 /
    # 0xC0000005 / 0xC0000374 之类。但所有 artifacts 此时都已写入磁盘，cli_v5
    # 端用 result.json 存在性 + record.status 判定成败，是稳健的。
    #
    # os._exit 跳过 atexit、threading 退出钩、缓冲 flush。前两者对我们无害，
    # 缓冲 flush 这里手动做。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
