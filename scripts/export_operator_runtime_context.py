# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(repo_root / 'scripts'))
    from operator_intent import build_runtime_context  # noqa: WPS433
    from ashare_control.control_plane import write_control_plane_snapshot  # noqa: WPS433

    ap = argparse.ArgumentParser(description='Export operator runtime context snapshot.')
    ap.add_argument('--repo-root', type=Path, default=repo_root)
    ap.add_argument('--output-path', type=Path, required=True)
    ap.add_argument('--control-plane-output-path', type=Path, default=None)
    ap.add_argument('--write-control-plane', action='store_true')
    args = ap.parse_args()

    payload = build_runtime_context(args.repo_root.resolve())
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.write_control_plane:
        write_control_plane_snapshot(
            args.repo_root.resolve(),
            output_path=args.control_plane_output_path,
            runtime_context_path=args.output_path.resolve(),
        )
    print(str(args.output_path))


if __name__ == '__main__':
    main()
