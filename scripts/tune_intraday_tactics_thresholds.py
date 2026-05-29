"""Grid-search intraday_tactics reason_thresholds against historical/snapshot context (no broker execution).

Loads one runtime config + tactical context, replays run_triggers + arbitrate for each threshold grid point,
scores by conflict/suppression vs accepted intents (tunable weights). Use for offline calibration before live.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_grid() -> Dict[str, List[Any]]:
    return {
        "take_profit_soft_pct": [0.025, 0.035, 0.045],
        "stop_loss_soft_pct": [0.018, 0.022, 0.028],
        "time_stop_minutes": [90, 120, 150],
    }


def _iter_grid(grid: Dict[str, List[Any]]) -> Iterable[Dict[str, Any]]:
    keys = [k for k, v in grid.items() if v]
    if not keys:
        yield {}
        return
    vals = [grid[k] for k in keys]
    for combo in itertools.product(*vals):
        yield dict(zip(keys, combo))


def _score(
    n_raw: int,
    n_win: int,
    n_conf: int,
    n_supp: int,
    w_conf: float,
    w_supp: float,
    w_win: float,
    w_idle: float,
) -> float:
    """Lower is better when conflicts/suppressions are costly and some activity is desired."""
    idle_penalty = w_idle if n_raw == 0 else 0.0
    return w_conf * n_conf + w_supp * n_supp - w_win * n_win + idle_penalty


def main() -> None:
    root = _repo_root()
    sys.path.insert(0, str(root / "src/ashare" / "src/ashare"))

    from engine.config_utils import load_config
    from engine.intraday_tactics.context_loader import load_tactical_context
    from engine.intraday_tactics.policy import load_tactical_policy
    from engine.intraday_tactics.priority_arbiter import arbitrate
    from engine.intraday_tactics.trigger_engine import run_triggers
    from engine.runtime_profiles import normalize_profile, profile_overrides
    from engine.trading_clock import clock_now
    from engine.config_builder import build_runtime_config

    parser = argparse.ArgumentParser(description="Offline grid search for intraday_tactics reason_thresholds")
    parser.add_argument("--config", default="", help="Runtime JSON path; if omitted, materialize via --profile")
    parser.add_argument("--profile", default="quick_test", help="Profile when --config omitted")
    parser.add_argument("--trade-date", default="", help="Override trade date (default: clock today)")
    parser.add_argument("--phase", default="tune_offline", help="Tactical phase label passed to run_triggers")
    parser.add_argument(
        "--grid-json",
        default="",
        help="Path to JSON object mapping threshold key -> list of values (Cartesian product)",
    )
    parser.add_argument("--w-conflict", type=float, default=10.0)
    parser.add_argument("--w-suppressed", type=float, default=3.0)
    parser.add_argument("--w-winner", type=float, default=0.15, help="Subtracted: rewards non-empty arbitrated sets")
    parser.add_argument("--w-idle", type=float, default=2.0, help="Penalty when trigger engine returns zero raw intents")
    parser.add_argument("--top", type=int, default=15, help="How many rows to print")
    parser.add_argument("--json-out", default="", help="Write best threshold patch + score to this path")
    args = parser.parse_args()

    if str(args.config).strip():
        cfg_path = Path(str(args.config).strip()).resolve()
        base = load_config(cfg_path)
    else:
        prof = normalize_profile(str(args.profile))
        base = build_runtime_config()
        for section, values in profile_overrides(prof).items():
            bucket = dict(base.get(section, {}) or {})
            bucket.update(values)
            base[section] = bucket

    td = str(args.trade_date or "").strip() or str(clock_now().date())
    ctx = load_tactical_context(base, trade_date=td)

    if str(args.grid_json).strip():
        grid = json.loads(Path(str(args.grid_json).strip()).read_text(encoding="utf-8"))
    else:
        grid = _default_grid()

    rows: List[Tuple[float, Dict[str, Any], int, int, int, int]] = []
    for patch in _iter_grid(grid):
        cfg = copy.deepcopy(base)
        it = cfg.setdefault("intraday_tactics", {})
        th = dict(it.get("reason_thresholds", {}) or {})
        th.update(patch)
        it["reason_thresholds"] = th
        policy = load_tactical_policy(cfg)
        raw = run_triggers(ctx=ctx, policy=policy, tactical_phase=str(args.phase), now=datetime.now())
        winners, conflicts, supp = arbitrate(raw)
        sc = _score(
            len(raw),
            len(winners),
            len(conflicts),
            len(supp),
            float(args.w_conflict),
            float(args.w_suppressed),
            float(args.w_winner),
            float(args.w_idle),
        )
        rows.append((sc, patch, len(raw), len(winners), len(conflicts), len(supp)))

    rows.sort(key=lambda x: x[0])
    print(f"trade_date={td} phase={args.phase} grid_keys={list(grid.keys())} trials={len(rows)}", flush=True)
    print("score\tpatch\tn_raw\tn_win\tn_conf\tn_supp", flush=True)
    for sc, patch, nr, nw, nc, ns in rows[: max(1, int(args.top))]:
        print(f"{sc:.4f}\t{json.dumps(patch, ensure_ascii=False, sort_keys=True)}\t{nr}\t{nw}\t{nc}\t{ns}", flush=True)

    best_sc, best_patch, _, _, _, _ = rows[0]
    out_doc = {
        "trade_date": td,
        "phase": str(args.phase),
        "best_score": best_sc,
        "reason_thresholds_patch": best_patch,
        "weights": {
            "w_conflict": float(args.w_conflict),
            "w_suppressed": float(args.w_suppressed),
            "w_winner": float(args.w_winner),
            "w_idle": float(args.w_idle),
        },
    }
    if str(args.json_out).strip():
        outp = Path(str(args.json_out).strip()).resolve()
        outp.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {outp}", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
