from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _package_root(repo_root: Path) -> Path:
    candidate = repo_root / "src" / "ashare"
    if (candidate / "engine").exists():
        return candidate
    return repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded non-structured evidence audit for the latest candidate pool.")
    parser.add_argument("--config", default="", help="Optional runtime config JSON path.")
    parser.add_argument("--candidate-pool", default="", help="Explicit candidate_pool.csv path.")
    parser.add_argument("--limit", type=int, default=0, help="Max symbols to audit. Defaults to config max_candidates.")
    args = parser.parse_args()

    repo_root = _repo_root()
    package_root = _package_root(repo_root)
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

    from engine.config_builder import build_runtime_config
    from engine.evidence_audit import run_evidence_audit

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig")) if str(args.config).strip() else build_runtime_config()
    result = run_evidence_audit(
        config,
        candidate_pool_path=Path(args.candidate_pool).resolve() if str(args.candidate_pool).strip() else None,
        limit=int(args.limit or 0) or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if bool(result.get("ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
