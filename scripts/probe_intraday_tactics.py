"""Lightweight probe: load runtime config and run intraday tactics pipeline without full chain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> None:
    root = _repo_root()
    runtime_root = root / "src" / "ashare"
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    parser = argparse.ArgumentParser(description="Probe intraday tactical layer artifact generation")
    parser.add_argument("--config", default="", help="Runtime JSON config path")
    parser.add_argument("--profile", default="quick_test", help="Profile to materialize config when --config omitted")
    parser.add_argument("--phase", default="probe_manual", help="Tactical phase label")
    parser.add_argument("--no-execute", action="store_true", help="Skip execution bridge")
    args = parser.parse_args()
    if str(args.config).strip():
        cfg_path = Path(str(args.config).strip()).resolve()
    else:
        from engine.config_builder import build_runtime_config
        from engine.runtime_profiles import normalize_profile, profile_overrides

        prof = normalize_profile(str(args.profile))
        cfg = build_runtime_config()
        for section, values in profile_overrides(prof).items():
            bucket = dict(cfg.get(section, {}) or {})
            bucket.update(values)
            cfg[section] = bucket
        cfg_path = runtime_root / "configs" / f"probe_intraday_tactics.{prof}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    from engine.intraday_tactics.runtime import run_intraday_tactics_pipeline

    out = run_intraday_tactics_pipeline(
        cfg_path,
        tactical_phase=str(args.phase),
        execute=not bool(args.no_execute),
        gate_only=False,
        trade_date="",
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out.get("ok", True) else 1)


if __name__ == "__main__":
    main()
