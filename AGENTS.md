# AGENTS.md

Before making changes in this repository, read `H:\Ashare\CODEX_DEV_LOG.md`.

## Required First Steps
1. Read `CODEX_DEV_LOG.md` first. It is the landing page that redirects to the split dev-log files.
2. Then read `CODEX_DEV_STABLE.md` for current truth.
3. Then read `CODEX_DEV_LOG_INDEX.md` for retrieval hints and `CODEX_DEV_UPDATES.md` for recent indexed history when needed.
4. Read `CLAUDE_CODEX_DIALOGUE.md` for any pending cross-AI notes.
5. Read `PROJECT_LAW.md` for the binding hard rules (net-zero, do-not-add-new-scheduler, etc).
6. Treat `CODEX_DEV_STABLE.md` as the current source of truth when older `README` files disagree.
7. Read these sections first in `CODEX_DEV_STABLE.md`: `Latest Stable Snapshot`, `Session Start Checklist`, `Known Dangerous Operations`, and `Known Issues`.
8. Check whether the user has explicitly allowed a long-running end-to-end run in the current session.
9. If you make code, config, runtime, data-path, or operational-rule changes, update all of these before ending the turn:
   - `CODEX_DEV_STABLE.md`
   - `CODEX_DEV_UPDATES.md`
   - `CODEX_DEV_LOG_INDEX.md`
   - Add a `CLAUDE_CODEX_DIALOGUE.md` entry if your change is something the other AI should know about

## Hard Operational Rules
- Do not run the full integrated pipeline or any full-cycle validation by default.
- The user has explicitly said full validation can run for hours and freeze the session.
- Default to lightweight checks only:
  - file inspection
  - targeted `Select-String`
  - targeted small commands
  - `python -m py_compile` on touched files
- **All new work lands in `H:\Ashare`**. The prior workspaces `F:\quant_data\AshareC#` and `F:\quant_data\Ashare` are now historical reference only — read-only.
- Do not modify files under `F:\quant_data\`. If you find yourself wanting to, stop and ask the user.
- Do not switch the Gmtrade bridge to the main Python environment. It must keep using the dedicated `gmtrade39` Python at `H:\Ashare\.venv\gmtrade39\Scripts\python.exe`.
- Do not echo API tokens or duplicate secrets into normal user-facing output unless explicitly asked. Secrets live in user env vars (`TUSHARE_TOKEN` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`), not in files.
- This workspace does NOT currently have any auto-push GitHub hook installed. If the user later sets one up, document it here.
- Site publishing / Google Drive mirroring / remote ollama tunnels / operator chat backend are all DROPPED in this workspace. Do not try to invoke them.

## Dev Log Maintenance
- `CODEX_DEV_LOG.md` is the landing file.
- `CODEX_DEV_STABLE.md` is the living stable handoff snapshot.
- `CODEX_DEV_UPDATES.md` is the indexed change history.
- `CODEX_DEV_LOG_INDEX.md` is the retrieval index for both.
- `CLAUDE_CODEX_DIALOGUE.md` is the cross-AI async note channel (Claude ↔ Codex).
- Future sessions must append or revise the doc set when they materially change:
  - entrypoints
  - configs
  - runtime profiles
  - data sources
  - execution behavior
  - operational warnings
  - validation policy
- If current truth changes, update the relevant stable sections before appending the historical change-log entry.
- Use the indexed entry format from `CODEX_DEV_UPDATES.md` unless there is a strong reason not to.
- Do not leave undocumented behavioral changes in code.

## Current Canonical Entry (H: workspace)
- Workspace operator entry: `H:\Ashare\launch_canonical.py`
- Wrapped business root: `H:\Ashare\main_research_runner.py`
- Trade clock entry: `H:\Ashare\trade_clock_service.py`
- Active runtime root: `H:\Ashare\src\ashare`
- Research Python: `H:\Ashare\.venv313\Scripts\python.exe` (Python 3.13.12)
- Broker Python: `H:\Ashare\.venv\gmtrade39\Scripts\python.exe` (Python 3.9.9 + gmtrade 3.0.6)
- If you are reasoning about the live code chain itself, inspect `main_research_runner.py`.
- If you are reasoning about the formal operator path, start from `launch_canonical.py`.

## Runtime Notes
- Default launcher mode is `integrated_supervisor`.
- Default launcher profile is `quick_test`.
- `quick_test` exists for minimal full-chain debugging, but it is still a real chain.
- `trade_clock_service.py` defaults to `daily_production` unless overridden.
- Formal runs started from `launch_canonical.py` should write `outputs\canonical_runs\<run_id>\run_manifest.json`.
- All runtime data writes under `H:\Ashare\data\`. Do not create any path under `F:\quant_data\` or `D:\AshareHotData\`.

## Net-Zero Rule (from PROJECT_LAW)
Before adding any new module / scheduler / gate / abstraction layer, you must first identify an old one to delete. AI collaborators violating this rule long-term will re-inflate the repo to the bloat that triggered the H: fork in the first place.
