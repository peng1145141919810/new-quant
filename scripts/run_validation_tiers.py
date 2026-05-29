"""Tiered lightweight validation: py_compile -> OMS merge unit tests -> optional intraday tactics probe."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _hub_root() -> Path:
    return _repo_root() / "src" / "ashare"


def _tier_compile(py_files: list[Path]) -> None:
    for p in py_files:
        if not p.is_file():
            raise FileNotFoundError(f"py_compile target missing: {p}")
        subprocess.check_call([sys.executable, "-m", "py_compile", str(p)])


def _tier_unittest(hub: Path, pattern: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(hub)}
    subprocess.check_call(
        [sys.executable, "-m", "unittest", pattern],
        cwd=str(hub),
        env=env,
    )


def _tier_probe(profile: str, no_execute: bool) -> None:
    probe = _repo_root() / "scripts" / "probe_intraday_tactics.py"
    cmd = [sys.executable, str(probe), "--profile", profile]
    if no_execute:
        cmd.append("--no-execute")
    subprocess.check_call(cmd, cwd=str(_repo_root()))


def main() -> None:
    root = _repo_root()
    hub = _hub_root()
    parser = argparse.ArgumentParser(description="Run validation tiers (compile, unittest, optional probe)")
    parser.add_argument("--max-tier", type=int, default=2, choices=(0, 1, 2), help="0=compile only, 1=+unittest, 2=+probe")
    parser.add_argument("--probe-profile", default="quick_test", help="Profile for tier-2 probe when --config omitted by probe")
    parser.add_argument(
        "--probe-execute",
        action="store_true",
        help="Allow probe to call execution bridge (default: probe uses --no-execute)",
    )
    args = parser.parse_args()

    compile_targets = [
        hub / "engine" / "clock_supervisor.py",
        hub / "engine" / "oms" / "tactical_merge.py",
        hub / "engine" / "config_builder.py",
    ]
    _tier_compile(compile_targets)
    print("tier0 py_compile: ok", flush=True)

    if args.max_tier >= 1:
        _tier_unittest(hub, "engine.oms.tests.test_tactical_merge")
        print("tier1 unittest tactical_merge: ok", flush=True)

    if args.max_tier >= 2:
        _tier_probe(args.probe_profile, no_execute=not args.probe_execute)
        print("tier2 probe_intraday_tactics: ok", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
