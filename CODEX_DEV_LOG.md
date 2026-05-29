# Codex Development Log

This file is now a landing page, not the full handoff body.

## Read Order
1. `CODEX_DEV_STABLE.md`
2. `CODEX_DEV_LOG_INDEX.md`
3. `CODEX_DEV_UPDATES.md`

## Purpose Split
- `CODEX_DEV_STABLE.md`: current operational truth only
- `CODEX_DEV_UPDATES.md`: indexed historical change log
- `CODEX_DEV_LOG_INDEX.md`: fast retrieval index for both files

## Non-Negotiable Rule
- If a session changes code, config, runtime paths, execution behavior, data dependencies, validation policy, or operator rules, it must update all three:
  - `CODEX_DEV_STABLE.md`
  - `CODEX_DEV_UPDATES.md`
  - `CODEX_DEV_LOG_INDEX.md`

## Current Canonical Entry
- Formal operator entry: `F:\quant_data\AshareC#\launch_canonical.py`
- Wrapped business root: `F:\quant_data\AshareC#\main_research_runner.py`
- Trade clock service: `F:\quant_data\AshareC#\trade_clock_service.py`
- Active Python runtime root: `F:\quant_data\AshareC#\src\ashare`

## Validation Policy
- Do not run the full integrated pipeline by default.
- Prefer file inspection, targeted probes, and `python -m py_compile` on touched files.

## Latest Doc Refresh
- Local time: `2026-04-10 02:00:36`
- Scope: split dev log into stable/history/index and rewrite primary entry docs against current workspace truth


## Latest Live Portfolio Snapshot
<!-- LIVE_PORTFOLIO_SNAPSHOT_START -->
- Updated at: `20260410_145514`
- Source report: `F:\quant_data\AshareC#\data\live_execution_bridge\execution_report_20260410_145514.json`
- Account: `4d74...2aa6`
- NAV: `0.0000`
- Cash: `0.0000`
- Positions: `0`
- Target names: `16`
- Orders/Fills: `0` / `0`
- Turnover raw/final: `0.0000` / `0.0000`
- Drift skipped: `0`
- Turnover adjustments: `0`
- Execution status summary: `success=0 partial=0 failed=0 skipped=0`
- Top holdings:
- 暂无持仓快照
<!-- LIVE_PORTFOLIO_SNAPSHOT_END -->
