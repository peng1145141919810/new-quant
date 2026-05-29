#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
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


if __name__ == '__main__':
    main()
