# Codex Stable Snapshot

## Document Contract
- This file is the stable operational truth for `H:\Ashare` (active workspace as of 2026-05-29).
- Historical session records live in `CODEX_DEV_UPDATES.md`.
- Fast retrieval pointers live in `CODEX_DEV_LOG_INDEX.md`.
- Cross-AI async notes between Claude and Codex live in `CLAUDE_CODEX_DIALOGUE.md` — read it at session start.
- When current truth changes, update this file first, then append an indexed entry to `CODEX_DEV_UPDATES.md`, then refresh the index file.
- The prior workspace `F:\quant_data\AshareC#` is now historical reference only — read-only, do not edit.

## Engineering Principles
- Root-cause-first: when a runtime, data, or orchestration problem is discovered, prefer fixing the authority path or underlying cause instead of stacking compatibility shims, silent fallbacks, or one-off patches that only mask the symptom.
- Shared authority over duplicated behavior: if two automation entrypoints are expected to keep data fresh, they must call the same refresh orchestration rather than carrying separate partially overlapping implementations.
- Fail visibly: if refreshes or runtime gates are important to correctness, emit explicit stage/state artifacts that show whether they ran, failed open, or blocked downstream work.

## Latest Stable Snapshot
- Snapshot time: `2026-05-31`
- Workspace root: `H:\Ashare` (active)
- Prior workspace root: `F:\quant_data\AshareC#` (historical reference, read-only)
- Major change this snapshot: full version-suffix naming cleanup across code + data dirs + docs. All V5/V5.1/V6 suffix tokens removed from module names, config keys, supervisor-state keys, stage ids, data directories, and documentation. See `CDL-20260531-049`.
- New governance doc: `CLAUDE_CODEX_DIALOGUE.md` (async Claude↔Codex note channel)
- Dialogue maintenance policy: `CLAUDE_CODEX_DIALOGUE.md` is an active-thread board, not a second dev log. Archive closed threads to `CLAUDE_CODEX_DIALOGUE_ARCHIVE.md` when it exceeds 25 messages, about 25 KB, or when long entries already have a CDL record; see `CDL-20260529-048`.
- Formal operator entry: `launch_canonical.py`
- Wrapped business root: `main_research_runner.py`
- Trade clock entry: `trade_clock_service.py`
- Active runtime root: `src\ashare`
- Formal run trace root: `outputs\canonical_runs` (does not yet exist on H:; will be created on first canonical run)
- Formal run manifest: `outputs\canonical_runs\<run_id>\run_manifest.json`
- Workspace default launcher mode/profile: `integrated_supervisor` / `quick_test`
- Trade-clock service default profile: `daily_production`
- Canonical data root in manifest: `data` (now under `H:\Ashare\data`, ~21.6 GB total)
- DROPPED: control-plane helper `ashare_control`, public portal source `site_portal`, site publish stage `outputs\site_publish_stage`, C# governance/orchestration skeleton (entire `csharp_runtime_skeleton`), Python RPC bridge `python_rpc_bridge.py`.
- Runtime state on H: is empty by design: no live release, no OMS ledgers, no execution reports, no trade-clock runtime state. First H:-side runs must regenerate these from scratch.

### H: Workspace Bootstrap Status (as of CDL-20260529-046)
- **DONE** Venvs installed on H:
  - `H:\Ashare\.venv313\Scripts\python.exe` (Python 3.13.12 + research deps)
  - `H:\Ashare\.venv\gmtrade39\Scripts\python.exe` (Python 3.9.9 + gmtrade 3.0.6)
- **DONE** `local_settings.py` rewritten — dead F: legacy overlay removed, H: venv paths set, cut modules disabled
- **DONE** All hardcoded `F:\quant_data\AshareC#` paths in source / configs / SYSTEM_MANIFEST batch-rewritten to `H:\Ashare`
- **DONE** Stale runtime artifacts purged (event_lake research state, generated_runtime configs)
- **DONE** `git init -b main`, first commit `ad19272`, 381 tracked files
- **DONE** Preflight green: `launch_canonical.py --preflight-only --profile quick_test` → exit 0, all 28 checks pass
- **N/A** `D:\AshareHotData\research_hub_integrated\runs` NVMe junction — no longer needed. H: is itself a dedicated NVMe SSD (aigo P7000Z 2TB), so the original motivation (move hot V5 writes off F:'s HDD onto NVMe) is solved by living on H: directly. V5 will write under `H:\Ashare\data\research_hub_integrated\` and that path is already on NVMe.
- **DONE** First H-side alpha methodology patch: V5.1 configs now default to `alpha_label_mode=cross_section_rank` and `feature_market_policy=exclude_from_stock_ranker`; see `CDL-20260529-047`.

### Physical Disk Layout (verified 2026-05-29)
- `C:` and `D:` — both partitions on Lexar THOR PRO 1TB NVMe SSD (shared spindle)
- `F:` — Seagate ST4000DM004 4TB HDD (slow; old workspace lives here, read-only)
- `H:` — **aigo P7000Z 2TB NVMe SSD, dedicated** — this is the active workspace. Do not assume H: is HDD.
- **PENDING** No real chain run executed yet — first `research_only` will likely hit data-consistency gate due to 20-day-stale SQLite (`research_data_v1` last touched 2026-05-09)
- **PENDING** `git remote add origin <url>` if user wants a remote; currently local-only repo

## Session Start Checklist
1. Read `CODEX_DEV_LOG.md`, then this file, then `CODEX_DEV_LOG_INDEX.md`.
2. Confirm whether the user has explicitly allowed any long-running end-to-end run in the current session.
3. Assume `F:\quant_data\Ashare` is read-only unless the user explicitly says otherwise.
4. Start reasoning from `launch_canonical.py` for governance flow and `main_research_runner.py` for runtime flow.
5. Treat `src\ashare` as the active Python runtime root, not the deleted repacked path family.
6. Read `CODEX_SECURE_OPS.md` before touching SSH-dependent deployment scripts, Git publishing scripts, or secret-adjacent runtime settings.
7. If you change behavior or operational truth, update this file, `CODEX_DEV_UPDATES.md`, and `CODEX_DEV_LOG_INDEX.md` before ending the turn.

## Workspace Identity
- This repository is the Rider / C# migration workspace copy.
- It now contains the active Python runtime tree under `src\ashare`.
- It still depends on machine-local runtime secrets and settings, especially `src\ashare\engine\local_settings.py`.
- The old live repo `F:\quant_data\Ashare` remains an operationally protected upstream and should be treated as read-only from this workspace.
- Large runtime data and generated artifacts are increasingly mirrored locally, but external backfills and legacy environment dependencies may still point back to the old machine layout.

## Canonical Runtime Chain
1. `launch_canonical.py`
2. Lightweight preflight via `tools\preflight_check.py` unless `--skip-preflight`
3. Canonical run registration via `tools\register_run.py`
4. Research Python resolved from `src\ashare\engine\local_settings.py`
5. `main_research_runner.py`
6. Runtime config generation into `src\ashare\configs\hub_config.runtime.<profile>.json`
7. Control-plane snapshot write into `site_portal\control_plane_snapshot.json`
8. Mode dispatch into `src\ashare\engine\...`

## Cross-Language Boundary
- Active business runtime is still Python and the C# skeleton still consumes many audit/state artifacts from disk.
- Active C# -> Python active invocation path now prefers a local loopback RPC host instead of direct stdout-only process coupling:
  - Python RPC host script: `python_rpc_bridge.py`
  - Default endpoint: `http://127.0.0.1:8765`
  - Health path: `/health`
  - Invoke path: `/invoke`
  - Runtime state path: `/runtime-state`
- `csharp_runtime_skeleton\src\Ashare.RuntimeSkeleton.PythonBridge\PythonProcessBridge.cs` now tries RPC first and falls back to direct process launch if the host cannot be reached.
- `csharp_runtime_skeleton\src\Ashare.RuntimeSkeleton.OperatorCli\Program.cs` now supports both legacy positional workspace-root usage and explicit `--workspace-root` / `-w` usage, so Python passthrough flags like `--preflight-only` no longer collide with CLI path parsing.
- This means active launch paths such as canonical run, trade clock run, summary run, and execution backend invocation are no longer forced to treat stdout as the only transport channel.
- JSON/CSV artifacts are still retained as audit and runtime state outputs; this change only upgrades the active invocation transport.
- Verified smoke path: `dotnet run ... canonical-run --workspace-root F:\quant_data\AshareC# --preflight-only --profile quick_test --mode execution_only` now reports `rpc_used: True`; current non-zero exit comes from Python preflight failure, not bridge/CLI parsing failure.

## Runtime State Query Layer
- `csharp_runtime_skeleton\src\Ashare.RuntimeSkeleton.Execution\RuntimeStateQueryService.cs` is now the single C# query surface for execution/runtime status aggregation.
- It centralizes reads for:
  - release contract
  - clock state
  - safety state
  - OMS summary and actual-state snapshots
  - `outputs\site_publish_stage\operator_runtime_context.json`
  - `outputs\site_publish_stage\control_plane_snapshot.json`
- `RuntimeStateAggregator` now resolves `release_id`, `trade_date`, `clock_phase`, `heartbeat_at`, `safety_mode`, and `gate_reason` through this query layer instead of directly scattering file reads in the aggregator.
- Current authority order is:
  - operator runtime context
  - control-plane snapshot
  - release / clock / safety / OMS artifacts
- The query layer now prefers one RPC bundle from `/runtime-state` for all of these:
  - `latest_release.json`
  - release manifest referenced by `latest_release.json`
  - target positions existence/path referenced by the release manifest
  - `clock_state.json`
  - `system_safety_state.json`
  - `oms_summary.json`
  - `latest_actual_portfolio_state.json`
  - `intent_ledger_latest.csv`
  - `order_ledger_latest.csv`
  - `fill_ledger_latest.csv`
  - `operator_runtime_context.json`
  - `control_plane_snapshot.json`
- If any of those cannot be obtained from the bridge, the corresponding C# services still fall back to local file reads.
- `status` / `doctor` can expose the active transport through `state_query_transport`; current mixed mode is `rpc_runtime_state+file_fallback`.
- `DesiredStateService`, `GapReportService`, and `OmsLifecycleService` are now also wired to the same query layer, so desired-vs-actual comparison paths no longer reintroduce separate release/OMS file scans as their primary truth source.
- OMS lifecycle capture now also prefers RPC-delivered ledger metadata for intent/order/fill availability and row counts before falling back to local CSV reads.
- This still does not remove file-backed state yet; it introduces a stable abstraction boundary so later migration to SQLite or broader RPC status queries can happen without changing the aggregator call sites again.

## Canonical Modes And Profiles

### Profiles
- `quick_test`: minimal real integrated debug profile, research cycles `1`
- `daily_production`: stable scheduled research/release profile, research cycles `3`
- `overnight`: heavy nightly research profile, research cycles `8`

### Modes
- Main modes: `integrated_supervisor`, `research_only`, `release_only`, `execution_only`, `midday_review_only`, `resume_downstream`
- Support modes: `oms_validate`, `full_cycle`, `ingest_only`, `extract_only`, `gap_only`, `industry_router_only`, `plan_only`, `bridge_only`, `intraday_tactics_only`, `evidence_audit_only`

## Trade-Clock Truth
- Service entry is `trade_clock_service.py`.
- It resolves runtime root the same way as the canonical launcher and writes a generated runtime config when `--config` is omitted.
- It runs lightweight preflight for `research_only`, `release_only`, `execution_only`, and `midday_review_only` before entering the clock loop.
- Latest trade-clock preflight status is written to `data\trade_clock\runtime\preflight_status.json`.
- Current default CLI profile in `trade_clock_service.py` is `daily_production`, which is different from the global launcher default `quick_test`.
- `scripts\start_trade_clock.ps1` reads `src\ashare\engine\local_settings.py` (and the example file) for Python resolution; it no longer points at deleted repacked `hub_v6` paths.
- `trade_clock.runtime_hot_reload` now defaults off (`TRADE_CLOCK_RUNTIME_HOT_RELOAD_ENABLED=False` in `local_settings.example.py`) so always-on supervisors are not torn down by hub-tree churn; turn it on only when you intentionally want config-driven reloads.
- Hot-reload fingerprinting ignores `__pycache__` and the embedded candidate paths for `local_settings` now resolve under `src\ashare` (the previous doubled `src\ashare\src\ashare` segment was incorrect).

## Runtime Configuration Surface
- Governance manifest: `SYSTEM_MANIFEST.yaml`
- Allowed profiles/modes: `RUN_PROFILES.yaml`
- Runtime config examples and generated configs: `src\ashare\configs`
- Strategy activation weights, overlay boosts, and priority blend are now config-backed under `strategy_activation` in the runtime config, not hard-coded only inside `strategy_activation.py`
- `derived_alpha_refresh` is now part of the pre-research refresh contract: it syncs affordable-store market/fundamental payloads into the runtime alpha tables that `strategy_activation.py` actually reads (`valuation_daily`, `crowding_daily`, `expectation_revision_daily`).
- `evidence_audit` is the boundary for non-structured web / announcement evidence. It runs on a small hard-data-selected candidate pool, fetches source pages, requires source IDs, and emits A/B/C/D/F evidence grades; portfolio generation only reads the latest audit grades as a gate/weight modifier and does not perform live web crawling inside portfolio construction.
- `evidence_audit_only` is now a first-class runtime mode in `main_research_runner.py` / `RUN_PROFILES.yaml`; `scripts\run_evidence_audit_once.py` is the lightweight manual wrapper. Integrated supervisor now defaults to running evidence audit after portfolio recommendation, rebuilding the portfolio after fresh audit grades, and blocking downstream execution if the audit stage fails (`EVIDENCE_AUDIT_RUN_AFTER_PORTFOLIO_RECOMMENDATION=True`, `EVIDENCE_AUDIT_REBUILD_PORTFOLIO_AFTER_AUDIT=True`, `EVIDENCE_AUDIT_BLOCK_EXECUTION_ON_FAILURE=True`).
- Candidate-pool formation is now hard-data-first by default: `PORTFOLIO_HARD_DATA_CANDIDATE_POOL_ENABLED=True` and the default selection weights use only seed weight, model prediction score, valuation signal, and liquidity signal. Router/thesis/event facts are no longer default structural-pool inputs; they belong in evidence audit unless explicitly re-enabled.
- GPU 研究脑 alpha-training knobs live under `strategy` in `src\ashare\research_brain\configs\hub_config.*.json`: `alpha_label_mode` defaults to `cross_section_rank`, and `feature_market_policy` defaults to `exclude_from_stock_ranker`. Training metrics use the transformed alpha label, while portfolio backtests still use the realized raw-return label recorded as `realized_return_label_col`.
- External source seed currently under top-level `configs`: `configs\external_sources\qianzhan_seed_urls.json`
- Industry-router configs: `src\ashare\configs\industry_router\*`
- Market-state config: `src\ashare\configs\market_state\default.json`
- Technical-confirmation config: `src\ashare\configs\technical_confirmation\default.json`
- T-overlay policy: `src\ashare\configs\t_overlay\t_audit_policy.json`
- Gmtrade runtime config examples and local config: `src\ashare\configs\gmtrade_runtime_config.*.json`
- Global-objective and EMS defaults now live in the generated runtime config under `global_objective` and `execution_management`; `config_builder.py` seeds those sections from `local_settings.py` / `local_settings.example.py`.
- `src\ashare\engine\local_settings.example.py` now prefers `\.venv313\Scripts\python.exe` for research when that venv exists, falls back to `\.venv\Scripts\python.exe` otherwise, and still keeps broker work on the separate Python 3.9 interpreter.
- Research Python dependencies for the canonical `.venv313` path are tracked in `src\ashare\requirements_v6_runtime.txt`; as of `2026-04-13` that list explicitly includes `pypdf` because the `announcement_fetchers -> pdf_utils` path imports `PdfReader` during `research_only` / trade-clock research startup.
- `ASHARE_DATA_ROOT` can redirect the logical data root, and `ASHARE_RESEARCH_PYTHON` can override the research interpreter without editing checked-in config.
- Current execution account semantics on this machine are:
  - `simulation`: purely simulated matching / mock execution path
  - `precision`: precision-matching paper account path on the Gmtrade side; this is still a simulated account operationally, not live capital
  - future real-money execution is intended to move to QMT / QM rather than reusing the current Gmtrade simulation bridge

## Artifact Registry

### Formal Governance Artifacts
- Canonical run manifests: `outputs\canonical_runs\<run_id>\run_manifest.json`
- Latest control-plane snapshot source: `site_portal\control_plane_snapshot.json`
- Publish-stage site bundle: `outputs\site_publish_stage`

### Trade-Clock And Intraday
- Trade-clock runtime preflight: `data\trade_clock\runtime\preflight_status.json`
- Clock account snapshot: `data\trade_clock\clock_account_snapshot.json`
- Intraday proxy latest root: `data\trade_clock\intraday_proxy\latest`
- Intraday tactics latest orders: `data\trade_clock\intraday_tactics\latest\intraday_tactical_orders.json`

### Release, OMS, And Execution
- Latest release family: `data\trade_release`
- OMS ledgers root: `data\live_execution_bridge\oms_v1\ledgers`
- OMS snapshots root: `data\live_execution_bridge\oms_v1\snapshots`
- Research run root: `data\research_hub_integrated\runs` (H: is NVMe; no junction needed).
- `src\ashare\engine\oms\runtime.py` now emits explicit `ok` / `status` fields in `execution_report_*.json`, and `src\ashare\engine\execution_bridge_runner.py` infers the same success contract for older report-shaped JSON payloads that omitted those fields.
- `src\ashare\engine\portfolio_release.py` now keeps `trade_date=today` as long as the current trading day still has at least one remaining execution window; it no longer flips to the next trading day immediately after the first morning window starts.
- `src\ashare\engine\portfolio_release.py` now treats filesystem release JSON as the primary release truth and SQL runtime artifacts as fallback/cache, so manual or automated file-side revocation is not overridden by stale SQL mirror rows.
- `src\ashare\engine\portfolio_release.py` rejects portfolio summaries with `simulation_ready=false` by default; only an explicit `trade_release.allow_not_ready_release=true` override may publish a not-ready release.
- `src\ashare\engine\portfolio_release.py` now carries optional evidence-audit summary/reviews/sources into each release directory and the `latest` release mirror when those artifacts exist.
- `src\ashare\engine\execution_manager.py` requires the active release manifest status to be `published` or `active`; `revoked`, `draft`, `failed`, or unknown release states block execution even when other gate fields look permissive.
- `src\ashare\engine\intraday_proxy_store.py` now treats Eastmoney minute K-line history as the default `rt_min` provider (`market_pipeline.rt_min_provider = eastmoney`) with Tushare as fallback; quote/list/tick proxy pulls remain on the existing Tushare crawler path.
- `src\ashare\engine\portfolio_recommendation.py` now emits `global_objective_snapshot.json` and `harvest_risk_assessment.json` beside `portfolio_recommendation.json` so downstream schedulers can consume normalized objective / adversarial signals without re-deriving them from the whole recommendation payload.
- `src\ashare\engine\portfolio_recommendation.py` also emits `econometric_guardrails.json`; `global_objective.py` now owns the unified builder that produces `econometric_guardrails + harvest_risk + global_objective` together instead of letting those layers drift separately.
- `src\ashare\engine\portfolio_recommendation.py` now propagates the latest V5 cycle deployment gate into recommendation readiness: when V5 says there is no champion, `research_deployment_ready=false` and `simulation_ready=false` are carried forward to release.
- `src\ashare\engine\execution_manager.py` now emits an EMS-layer decision artifact under `data\trade_clock\ems\<namespace>\<timestamp>\execution_management_decision.json` and carries `global_objective`, `harvest_risk`, and EMS posture into the execution dispatch chain.
- `src\ashare\engine\intelligent_scheduler.py` now treats the unified objective bundle as a real arbitration input: guardrail / evidence / harvest hard flags can downgrade the final execution verdict to `proceed_degraded` or `reduce_only`, not just annotate the audit trail.
- `src\ashare\engine\supervisor.py` and `src\ashare\engine\objective_scheduler.py` now also centralize the research side under one scheduler authority:
  - local performance / regime logic is now signal-only (`authority_role=local_signal_only`) instead of directly writing final strategy or portfolio overrides
  - the scheduler now emits `research_scheduler_decision_v2` with `advisor_chain`, `reason_chain`, `route_budget`, `strategy_overrides`, and `portfolio_overrides`
  - research scheduler artifacts now write both `latest_research_budget_decision.json` and `latest_research_scheduler_verdict.json`
- Research feedback is now size-bounded at the scheduler boundary: `objective_scheduler.py` skips feedback JSON inputs larger than `5 MB`, compacts advisor/signal feedback before writing scheduler artifacts, and stores only compact scheduler decision summaries in `performance_feedback.json` instead of recursively embedding the full prior decision tree.
- `src\ashare\research_brain\hub\candidate_factory.py` now obeys the scheduler's allowed / banned model-family list even on routes that previously hard-coded `xgboost_gpu` or `generated_family`.
- `src\ashare\research_brain\hub\candidate_factory.py` also writes compact bridge-input snapshots into `cycle_plan.json`; oversized bridge input JSON files are skipped instead of being copied into every candidate-plan artifact.

### SQL Stores
- Runtime research SQLite: `data\sql_store\research_data_v1.sqlite3`
- Research fact layer SQLite: `data\sql_store\research_fact_layers_v1.sqlite3`
- Affordable data SQLite: `data\sql_store\affordable_data_v1.sqlite3`
- Unified per-source fetch log table: `data\sql_store\research_fact_layers_v1.sqlite3::source_fetch_run_log`
- As of `2026-05-09 12:52`, a direct derived-alpha refresh from `affordable_data_v1.sqlite3` updated runtime alpha tables in `research_data_v1.sqlite3`: `valuation_daily` and `crowding_daily` now reach `2026-05-08`; `expectation_revision_daily` now has affordable-derived forecast/express rows through `2026-05-07`.

## Current Architecture Truth
- Python business runtime lives under `src\ashare\engine`.
- C# skeleton under `csharp_runtime_skeleton` is for pathing, authority models, CLI observability, and Python bridge orchestration, not direct strategy replacement.
- Control-plane snapshot generation is centralized in `ashare_control\control_plane.py`.
- `strategy_activation.py` now reads named weight groups from config and writes the effective weight set into the activation summary, so activation/ranking constants are auditable instead of buried only in code.
- The active engine surface includes dedicated modules for:
  - `candidate_pipeline.py`
  - `portfolio_construction_pipeline.py`
  - `strategy_activation.py`
  - `evidence_audit.py`
  - `clock_phase_registry.py`
  - `remote_clock_delegate.py`
  - `constraint_brain.py`
  - `llm_trace.py`
  - `llm_operating_brain.py`
  - `intraday_tactics\`
  - `intraday_state_machine\`
  - `market_state\`
  - `oms\`
  - `portfolio\`
- V5.1 candidate codegen under `src\ashare\research_brain\hub\codegen.py` no longer asks the LLM to emit free-form Python modules directly.
- Current candidate-lab flow is now:
  - each provider attempt first emits a lightweight structured `intent`, then emits the final structured JSON spec for `feature_pack`, `train_override`, or `generated_model`
  - local validators enforce schema, naming, numeric bounds, and allowed feature-formula helpers
  - a deterministic local compiler renders those validated specs into Python modules in the candidate lab
  - compiled modules still go through compile/import validation before the candidate can run
  - spec repair remains bounded and is now provider-tier aware: local Ollama first, DeepSeek next, OpenAI last when configured and reachable
- The V5.1 `llm_brain` config surface now supports `provider_tiers`, so codegen can escalate by cost/quality tier instead of hardwiring one provider:
  - typical order is `local_ollama` -> `deepseek` -> `openai`
  - legality is judged locally by schema validation plus compiled-module validation, not by model self-report
  - unresolved invalid specs still fall back to deterministic baseline specs after tier exhaustion
- Local Ollama shared clients now short-circuit after recent healthcheck failure or timeout instead of repeatedly blocking:
  - `llm_router.LocalOllamaChatClient` performs a cached `/api/tags` health probe before role calls
  - recent timeout/unreachable states enter a short cooldown and return `service_cooldown` / `service_unavailable`
  - the event-extract-specific local worker applies the same cooldown pattern before retrying `/api/chat`
- V5.1 generated model specs now canonicalize common tree-model aliases before legality checks:
  - `xgboost` / `xgb` family hints normalize into supported local families
  - `n_estimators`, `num_trees`, and `eta` normalize into local schema keys
  - tree-style specs carrying `min_child_weight` can reroute into `extra_trees` and map that weight into `min_samples_leaf`
- V5.1 training-override specs now also normalize the most common low-cost-tier schema drifts before legality checks:
  - `sample_weight_mode` aliases like `balanced` / `uniform` normalize into supported local values
  - ratio-style `feature_cap` values can normalize into an integer cap using local context
  - pair-style `clip_label_quantile` values like `[0.05, 0.95]` normalize into the local single-sided quantile form
- Candidate labs now store both spec and compiled-module artifacts:
  - `feature_pack.spec.json` + `feature_pack.py`
  - `train_override.spec.json` + `train_override.py`
  - `generated_model.spec.json` + `generated_model.py`
  - validation, selected provider intent, and repair history in `workspace_validation.json`
- `src\ashare\research_brain\hub\training_engine.py` treats unresolved invalid generated artifacts as a candidate-level skip (`budget_action=skip_invalid_codegen`) before training starts, so one bad generated candidate does not crash the whole V5.1 batch.
- `src\ashare\research_brain\hub\training_engine.py` and `single_run_v5.py` now backtest directly from the in-memory `pred_test_df` instead of forcing a full `pred_test.csv` round-trip to disk first.
- `src\ashare\research_brain\hub\portfolio_engine.py` no longer treats same-day signal rows as same-day executable fills in backtests; it now models next-bar-close entry, blocks suspended / limit-up entry, and defers exit until the first sellable bar after the holding horizon instead of blindly consuming `future_ret_*`.
- `src\ashare\research_brain\hub\training_engine.py` now derives an execution-aligned realized-return label (for example `future_ret_5__entry_lag_1`) from `close`, then can derive a separate alpha-training label from that realized return. The current default is daily cross-sectional rank (`alpha_label_mode=cross_section_rank`); date splits still include an embargo of `label_horizon + execution_lag_bars` trading days, and `train_summary.json` records `time_alignment`, `alpha_label_meta`, `feature_policy_meta`, realized-return metrics, and `overfit_diagnostics`.
- V5.1 stock-ranker training now treats market-index variables as regime/risk inputs rather than direct stock-picking features by default: `feature_market_policy=exclude_from_stock_ranker` removes direct `hs300_*`, `index_ret_*`, `market_ret_*`, `market_beta_*`, and `benchmark_*` features from the selected training feature list while leaving portfolio risk logic free to read market-state columns from prediction frames.
- `src\ashare\research_brain\hub\portfolio_engine.py` is now cost-aware as well as limit-aware: the default V5 paper execution model applies configurable buy/sell fees, stamp tax, base slippage, and an extra queue-risk penalty on extreme up/down bars (`next_bar_close_cost_aware_limit_aware_exit`).
- V5 latest-score and portfolio construction now retain the raw `amount` column and evaluate liquidity through `liquidity_amount_cny`, which prefers `amount_mean_20` when present, falls back to same-row `amount`, and scales small Tushare-style amount values by `1000` before comparing with the minimum liquidity threshold.
- `src\ashare\engine\data_consistency_guard.py` is now the automation freshness gate for research/release execution. `clock_supervisor.py` and `supervisor.py` call it to block stale or mismatched datasets when today's refreshes are missing, and the gate payload is written into phase artifacts. This is the explicit protection against missing overnight news because morning `research_refresh` / `release_refresh` must now prove they refreshed today's external/fact datasets before the trading-day phases continue.
- Trade-clock morning `research_refresh` now defaults to the scheduler's production profile (`daily_production` on this machine), not `quick_test`, so overnight rebuilds no longer silently regenerate only a minimal debug research set before `release_refresh`.
- `src\ashare\engine\market_pipeline.py` now mirrors `HS300` daily rows and `market_price_snapshot` rows into `data\sql_store\research_data_v1.sqlite3` whenever those file-backed artifacts are refreshed, so the runtime SQLite no longer leaves those tables permanently stale relative to the file layer.
- `src\ashare\engine\supervisor.py` and `src\ashare\engine\clock_supervisor.py` now share one pre-research refresh orchestration entry (`run_pre_research_refresh_bundle(...)`) for `market_pipeline_refresh + affordable_data_bundle + external_research_refresh + research_fact_refresh`. The market refresh runs before the data-consistency gate, and `supervisor.py` reuses that result in the later `market_pipeline` stage instead of duplicating the refresh in the same process.
- The refresh scripts `update_affordable_data_bundle.py`, `build_event_fact_layer.py`, `build_industry_hard_factor_layer.py`, and `update_external_research_feeds.py` now bootstrap the active runtime from `src\ashare` instead of the deleted historical `src\ashare\src\ashare` path, and the `customs_summary` helper loader now registers its dynamic module in `sys.modules` before execution so its dataclass-based parser can load correctly.
- A direct validation run on `2026-04-11 22:40` confirmed the shared pre-research refresh bundle now completes successfully end-to-end on this machine (`affordable_ok=true`, `external_ok=true`, `research_fact_ok=true`) with no observed rate-limit / timeout / permission-denied signatures in the refresh logs.
- The major refresh producers now write one unified daily source-fetch journal into `source_fetch_run_log` inside `research_fact_layers_v1.sqlite3`. Current writers are:
  - `scripts\update_affordable_data_bundle.py`
  - `scripts\update_external_research_feeds.py`
  - `scripts\build_event_fact_layer.py`
  - `scripts\build_industry_hard_factor_layer.py`
  - `src\ashare\engine\market_pipeline.py`
- `scripts\build_industry_hard_factor_layer.py` no longer trusts only the hard-coded article URLs in `source_contracts.json`; it now attempts a bounded discovery step for official-page sources and records the resolved URL, freshness, and stale/failure state into `source_fetch_run_log`.
- When industry-router official-page discovery rules are ambiguous, `industry_router.core.source_ingest.resolve_official_page(...)` now escalates to an LLM adjudication pass over the top candidate pages instead of silently accepting the best regex score. The LLM path is config-backed under `industry_router.source_fetch.llm_*`, and any LLM-picked result is recorded into `source_fetch_run_log.extra_json`.
- Full `pred_test.csv` emission is now opt-in via `ASHARE_WRITE_FULL_PRED_TEST_CSV=1`; otherwise the runtime writes at most a bounded `pred_test.sample.csv` controlled by `ASHARE_PRED_TEST_SAMPLE_ROWS` for debug inspection.
- Research authority is now centrally unified beyond execution:
  - `objective_scheduler.py` is now the single producer of final research-budget, route-budget, model-family, strategy, and portfolio override decisions for the supervisor path
  - `supervisor._build_strategy_feedback(...)` now produces local evidence / route signals / constraint signals only
  - the merged `performance_feedback.json` is now the scheduler's final carrier surface, not a local module override surface
- `supervisor.py` rebuilds market-state artifacts with `build_market_state_artifacts(...)` immediately before objective scheduling after the pre-research market refresh; it falls back to `load_latest_market_state(...)` only when the rebuild produces no payload. The scheduler should no longer budget against a stale prior-date `market_state.json` after fresh market tables were just written.
- `candidate_factory.py` no longer gets to bypass the scheduler by forcing disallowed `xgboost_gpu` / `generated_family` selections on `model`, `data`, or `hybrid` routes, and non-model routes no longer inherit a banned parent model family.
- Portfolio recommendation and V2A portfolio-control stages now defensively tolerate missing optional candidate columns such as `is_existing_position`, `event_fact_backed`, `router_allow_entry`, `tech_allow_entry`, `current_weight_ref`, and `portfolio_weight` instead of assuming `DataFrame.get(...).fillna(...)` always returns a Series.
- A bounded manual end-to-end smoke has now been verified across:
  - `research_only` producing fresh V5.1 output
  - manual portfolio recommendation regeneration
  - `release_only` publishing a fresh release
  - `execution_only --execution-mode simulation` producing OMS ledgers, control feedback, research-meta feedback, and execution report artifacts
  - `plan_only` reloading `oms_v1\feedback\research_meta_feedback_latest.json` into `research\context_pack\research_context_pack.json`
- The system is no longer truthfully described by the old `hub_v6` or repacked-root naming as a runtime root, even though some compatibility references still exist in helper code.

## Operational Rules
- Do not run the full integrated pipeline by default.
- Use lightweight validation first: file inspection, targeted commands, `python -m py_compile`, and small probes.
- Do not modify `F:\quant_data\Ashare` from this workspace.
- Keep the heaviest V5 run artifacts on NVMe when possible; on this machine only `data\research_hub_integrated\runs` is intentionally hot-moved to `D:` and the broader `data` tree remains on `F:` to conserve SSD capacity.
- Do not switch the broker bridge to the main Python environment; keep `GMTRADE_PYTHON_EXECUTABLE` on the dedicated Gmtrade / 掘金 adapter interpreter (on this machine the template points at Python 3.9; prefer a dedicated `gmtrade39` venv `Scripts\\python.exe` when you maintain one).
- Do not echo secrets into user-facing output.
- Do not re-enable full `pred_test.csv` emission unless you are doing bounded debugging; the old default was a major HDD / storage-stack pressure source during the crash investigation.
- Do not reintroduce full nested scheduler decisions or full bridge feedback payloads into active feedback artifacts. Scheduler / bridge feedback JSON inputs are intentionally capped at `5 MB`; large historical runtime artifacts should be treated as tombstoned audit remnants, not active inputs.
- Do not publish or execute a portfolio when the latest V5 cycle deployment gate says there is no champion. `portfolio_recommendation.py` now lowers `simulation_ready`, `portfolio_release.py` refuses the release by default, and `execution_manager.py` blocks non-published release statuses.
- Treat `data\trade_release\latest_release.json` and the referenced release manifest files as the active release truth. SQL runtime artifact rows are fallback/cache, not authority for revocation or readiness.
- Unless the user explicitly asks for mock matching, default manual execution probes to the runtime default `precision` account mode instead of overriding to `simulation`.
- Treat `precision` here as the default paper-trading / precision-matching account, not as live trading.
- Treat `simulation` and `precision` as different paper-execution semantics; do not conflate either of them with the future QMT / QM real-money path.
- The interpreter behind `GMTRADE_PYTHON_EXECUTABLE` must have the `gmtrade` package installed; otherwise the health probe and OMS bridge fail before login with `ModuleNotFoundError: No module named 'gmtrade'`.
- Secret-adjacent operating notes now live in `CODEX_SECURE_OPS.md`; use that file for SSH/credential workflow memory without storing secret values.
- Watch for auto-push behavior on commit because a local post-commit hook may publish automatically.

## Known Dangerous Operations
- Running `launch_canonical.py` without `--preflight-only` starts a real runtime chain.
- `quick_test` is still a real integrated path, not a trivial smoke test.
- `trade_clock_service.py` can enter an always-on loop; use `--once` for bounded inspection.
- `execution_only` without gating or shadow protections can reach the broker bridge.
- `release_only` can rewrite latest release pointers and downstream execution truth.
- `release_only` should now fail closed when the current portfolio recommendation is not research/simulation ready; this is expected, not an incidental runtime failure.
- Site publish scripts can replace contents under `outputs\site_publish_stage` and publish targets.

## Known Issues
- `README.md` and several legacy sub-readmes had mojibake and stale path assumptions before this rewrite; older copies should not be trusted over this file.
- `tools\preflight_check.py` still contains legacy import targets under `hub_v6.*`; treat it as a lightweight guardrail, not a full proof that naming migration is complete.
- The 2026-04-11 crash investigation found repeated native failures under Python **3.14** (`python314.dll` `0xC0000005`, `ntdll.dll` `0xC0000374`) during V5 GPU runs, followed later by a system `MEMORY_MANAGEMENT (0x1A)` bluescreen and `RstMwService.exe` crash. The workspace now defaults research execution to Python **3.13** (`.venv313`) while leaving `GMTRADE_PYTHON_EXECUTABLE` on Python **3.9**.
- On this machine, `C:\Users\Administrator\AppData\Local\Programs\Python\Python39\python.exe` now has `gmtrade 3.0.6` installed and passes the health probe again. If broker health regresses on another machine, verify that exact interpreter first before debugging token/account settings.
- `sql_store.sqlite_connection` enables WAL, `busy_timeout`, and a longer busy wait on connect; `market_pipeline.sync_enriched_daily_from_tushare` holds one SQLite connection for the whole enriched sync to cut down `database is locked` races when the trade clock or RPC bridge touches `research_data_v1.sqlite3` at the same time.
- The earlier automation split where `integrated_supervisor` refreshed only `market_pipeline` while `clock_supervisor` separately owned `affordable_data_bundle + external_research_refresh + research_fact_refresh` is now removed; both paths share the same pre-research refresh bundle. If databases are still stale after this point, the cause is the refresh jobs themselves not being run successfully or their local source files already being old, not entrypoint divergence.
- As of `2026-04-12`, automation also enforces a data-consistency gate before research/release-sensitive phases. If the current trade date does not have same-day refresh evidence for `affordable_data_refresh`, `external_research_refresh`, `research_fact_refresh`, and `industry_hard_factor_refresh`, the morning chain now fails closed instead of quietly trading on a previous-evening snapshot and missing overnight news.
- As of `2026-05-09`, the explicit user-authorized repair run refreshed market data through trade date `2026-05-08`: run `20260509_000722_b2aef930` updated `market_enriched_daily`, `market_hs300_daily`, `market_price_snapshot`, and training rows after the market refresh was moved before the data-consistency gate. Older SQL freshness notes below are retained as historical context, not current market-table truth.
- Historical SQL freshness audit from `2026-04-11 20:35`:
  - `data\sql_store\research_data_v1.sqlite3`: after the market-pipeline mirror fix and validation run, `market_enriched_daily` / `market_hs300_daily` / `market_price_snapshot` are all at `2026-04-10`; this appears consistent with the latest trade date currently available to the local market source path during the validation run
  - `data\sql_store\affordable_data_v1.sqlite3`: after the script-bootstrap fix and direct validation run, affordable datasets are refreshing again and recent primary dates moved up to `2026-04-10` / `2026-04-11`
  - `data\sql_store\research_fact_layers_v1.sqlite3`: after the unified fetch-log rollout and direct validation runs, `qianzhan_indicator_daily`, `event_fact_company_actions`, and the unified `source_fetch_run_log` are advancing on `2026-04-11`; `industry_factor_operation_daily` is no longer a pure scheduling issue, its root cause is that several `industry_router` official sources still point at stale topic pages or need tighter source-specific discovery rules. Use `source_fetch_run_log` instead of guessing: it now shows which sources are fresh, stale, failed, or returning zero rows.
- As of `2026-04-11 23:20`, `industry_hard_factor_refresh` is only partially repaired:
  - the root cause was confirmed to be static article URLs in `src\ashare\configs\industry_router\source_contracts.json`
  - the refresh now emits explicit per-source freshness / stale / failure logs into `source_fetch_run_log`
  - some official sources still require tighter source-specific discovery because generic landing pages or irrelevant bulletin pages can outrank the intended article without stronger rules; current visible offenders include the two `gov_*` macro pages, the two `pbc_*` macro pages, and several `miit` / `stats` pages that still need discovery hardening
- As of `2026-04-11 23:30`, the official-page picker has an LLM fallback, but the acceptance rule is still conservative by design: if regex scoring plus LLM adjudication still cannot identify a non-generic article page with enough confidence, the refresh records failure/zero-row status instead of forcing a likely-wrong page into `industry_factor_operation_daily`.
- If `PYTHON_EXECUTABLE` is left as a placeholder in a private overlay, the fallback is still `PATH` `python.exe`, which may lack `pypdf` and other research deps; install `src\ashare\requirements_v6_runtime.txt` on that interpreter or set an explicit path.
- A canonical `research_only` run on `2026-04-13` reached V6 planning and entered V5.1 GPU research only after `pypdf` was installed into `.venv313`; before that, the run failed in `engine.pdf_utils` with `ModuleNotFoundError: No module named 'pypdf'`.
- The `2026-04-13` oversized feedback incident was isolated on `2026-05-08`: scheduler decisions embedded prior `bridge_feedback` inside `advisor_chain` / `signal_snapshot`, then `merge_signals_with_budget_feedback(...)` wrote the full decision back to `performance_feedback.json`, so each later scheduler pass recursively copied the previous decision tree. The active code now compacts scheduler feedback and skips feedback JSON inputs larger than `5 MB`.
- Oversized generated runtime JSON artifacts from that recursion were replaced with small tombstone JSON files on `2026-05-08` instead of being backed up byte-for-byte. The tombstoned artifacts include `data\event_lake_v6\bridge\performance_feedback.json`, `data\event_lake_v6\research\supervisor\performance_feedback.json`, `data\research_hub_integrated\cycles\cycle_001_20260413_081008\cycle_plan.json`, and objective-scheduler JSON files larger than the new input limit.
- `scripts\build_industry_hard_factor_layer.py` had a real runtime bug on `2026-04-13`: `_build_official_metric_rows(...)` referenced `config` without accepting it, so `industry_hard_factor_refresh` wrote only failed `source_fetch_run_log` rows with `NameError: name 'config' is not defined` and the automation data-consistency gate blocked `research_refresh`.
- `src\ashare\engine\data_consistency_guard.py` also needed a trading-day alignment fix on `2026-04-13`: `market_price_snapshot` can legitimately be on the current trade date while `market_enriched_daily` / `market_hs300_daily` are still on the previous trading day pre-open (for example Monday vs prior Friday). The guard now computes `market_table_spread_days` from the daily tables only instead of treating the same-day snapshot as a table-mismatch failure.
- `src\ashare\engine\clock_supervisor.py` had two scheduler-state bugs on `2026-04-13`:
  - same-day `research_refresh` / `release_refresh` failures caused by a temporary data gate failure could not be retried before `preopen_gate`, so the morning chain had no recovery path after the underlying refresh bug was fixed in-process
  - queued intraday phases (`preopen_gate`, `simulation`, `midday_review`, `afternoon_execution`, tactical phases) had no expiry check, so restarting the trade-clock after their window had passed could incorrectly replay stale same-day phases after market close
- `scheduler_runtime.json` previously retained stale `stop_reason`, `reload_reason`, and active-phase metadata across fresh trade-clock starts because startup payloads only merged state; startup now clears those fields explicitly so runtime state is no longer misleading after a restart.
- The latest strict research run family on `2026-04-13` is not failing syntactically, but the best observed candidate remains only moderate (`lightgbm_gpu`, annualized ~10.6%, Sharpe ~0.54, MDD ~-28%). The largest structural reason found in this session was not just alpha strength: `amount_mean_20` was null for virtually all `MAIN/GEM/STAR` rows, so the old liquidity filter collapsed the investable universe to BSE-heavy output. The runtime mitigation now uses normalized `liquidity_amount_cny` with raw `amount` fallback, but the upstream market-data issue remains: enriched history still has gaps and mixed amount units, so source-level backfill/unit normalization is still required before research metrics should be treated as final.
- On `2026-05-09`, quick-test and daily-production research no longer reproduced the earlier V5 native crash path: quick run `20260509_000722_b2aef930` completed one V5 cycle, and daily-production run `20260509_012554_b1d1ce42` completed three V5 cycles / thirty-six candidates. This does not mean the research is deployable: every cycle still failed the V5 deployment gate with no champion.
- The current macro research blocker is alpha/evidence quality, not only plumbing. The best daily-production candidate in run `20260509_012554_b1d1ce42` had total score about `12.48`, Sharpe about `0.94`, max drawdown about `-22.5%`, and no deployment approval. The portfolio objective layer also reported hard flags for evidence below floor, family concentration above ceiling, and incremental value below floor. A later root-cause pass found that this was worsened by stale runtime alpha tables and front-end feature-column shadowing; those two plumbing defects are now fixed, but event/order/industry evidence coverage remains weak and should still gate deployment.
- A static alpha diagnosis on `2026-05-29` found the best observed V5.1 candidate's feature importance dominated by `hs300_*` market variables and confirmed the training target was still raw absolute return. The active mitigation is configuration-driven rather than a new scheduler: train the stock-ranker on cross-sectional alpha labels and exclude direct market-beta features from that ranker, while keeping raw realized returns for backtest/economic evaluation.
- `strategy_activation.py` no longer treats missing or constant feature columns as neutral `0.5` evidence. Constant/missing normalized signals now contribute `0.0`, all-zero rows become `activation_alpha_family=unclassified`, and pre-existing empty candidate-pool feature columns are dropped before fresh SQL features are merged.
- LLM usage is now intentionally narrower by default: `ENABLE_STRATEGY_ACTIVATION_LLM=False` and `ENABLE_PORTFOLIO_CANDIDATE_LLM_REVIEW=False` in the example/runtime defaults, so LLMs no longer directly favor symbols/families in the active ranking path unless explicitly re-enabled. Use `evidence_audit.py` for LLM-reviewed non-structured evidence, with source IDs retained for every claim.
- Strategy activation meta weights are now conservative by default: valuation and liquidity carry the structural signal weight, revision is small, and order-flow / event-drive / industry scores default to `0.0` until non-structured evidence audit or explicit configuration promotes them.
- Release `release_20260509_011316_90218762` was created by a quick-test run before the V5 deployment gate was propagated into portfolio/release readiness. It is now explicitly marked `revoked`; execution gate probes return `release_status_revoked`.
- A release-file vs SQL-runtime-artifact dual-authority problem was observed on `2026-05-09`: a revoked file-side release could still appear executable if stale SQL artifact rows were preferred. Release loading is now file-first with SQL fallback, but broader duplicated-authority surfaces should still be treated cautiously when debugging state disagreement.
- Historical `execution_report_*.json` files created before `2026-04-10 15:35` may omit explicit `ok` / `status`; use the bridge parser or inspect the rest of the report structure before treating those older artifacts as hard execution failures.
- Releases published after all configured execution windows for the day still roll forward to the next trading day; the fix only changes the earlier misclassification of midday manual releases when an afternoon execution window still remained.
- Eastmoney intraday-minute integration currently covers the `rt_min` / minute-bar path only; it does not yet replace the Tushare-backed realtime quote/list/tick proxy endpoints.
- Research-budget orchestration is now centralized in `objective_scheduler.py`, quick-test now prefers `ridge_ranker` plus `lightgbm_gpu`, and a direct single-candidate validation on `2026-04-11 16:03` proved `effective_model_family=lightgbm_gpu` and `gpu_used=true`.
- EMS is now a distinct execution-policy layer in Python (`execution_ems.py`), but it is still advisory-to-bridge rather than a fully separate long-lived intraday controller service or a QMT / QM execution adapter.
- The bounded end-to-end smoke recorded earlier hit the `simulation` account only because the command explicitly passed `--execution-mode simulation`; the workspace default `quick_test` runtime config and execution-policy default are already `precision`, and `execution_bridge_runner` correctly maps that to the `account_profiles.precision` account id / alias.
- `tools\preflight_check.py` and `scripts\run_validation_tiers.py --max-tier 1` now pass against the active `src\ashare` runtime root; the earlier `hub_v6.*` and doubled `src\ashare\src\ashare` blockers were naming debt and have been corrected.
- Full canonical integrated runs attempted on `2026-04-10 22:50`, `2026-04-10 23:19`, `2026-04-10 23:54`, and `2026-04-11 15:04` all reached the V5.1 research child under `integrated_supervisor + quick_test`, but the child still terminated natively before portfolio recommendation / release / execution completion:
  - run `20260410_225047_603ac68d`: exit `3221225477`
  - run `20260410_231921_18245dda`: exit `3221225477`
  - run `20260410_235405_999e50a0`: after banning `xgboost_gpu` / `generated_family` in quick-test scheduling, candidate logs showed `lightgbm_auto` and `ridge_ranker`, but the child still died with exit `3221226356`
  - run `20260411_150409_6318030b`: V5.1 progressed to candidate `12/12`, wrote `pred_test.csv` for `candidate_012_0feb4e6f`, then the child still died with exit `3221226356` before `controller_state.json` / `adaptive_loop_final.json` were refreshed.
- run `20260411_161117_e1dddd38`: after restoring real GPU routing, quick-test candidate logs showed `lightgbm_gpu` on candidate `1` and candidate `4`; candidate `4` (`20260411_163304_c2d2568e`) wrote `train_summary.json` with `gpu_used=true` and `effective_model_family=lightgbm_gpu`, plus a large `pred_test.csv`, then the V5.1 child still died with native exit `3221225477` before `run_summary.json` was written.
- The remaining V5.1 failure is now narrowed to the late candidate execution / batch-finalization path rather than early banned-family selection; candidate `012` is the current focal repro.
- After GPU restoration, the focal repro shifts earlier to candidate `004` on the GPU path; the current crash still occurs after training metrics and `pred_test.csv` write, and before latest-portfolio / backtest / final run-summary completion.
- Active naming debt still exists mostly in helper docs or generated configs, but the live launcher/runtime path no longer depends on repacked `hub_v6` roots.
- Some helper scripts and docs still use historical terminology from deleted repacked roots or old website deployment flow.
- The workspace is a dirty tree with many unrelated modifications; do not assume uncommitted files belong to the current task.
- The new structured-spec compiler reduces syntax-fragment failures, but unresolved failures are still possible when the LLM repeatedly emits invalid specs or asks for unsupported formula helpers / model params; those candidates now skip with explicit validation diagnostics instead of taking down the full batch.
- Current smoke verification on this machine shows the tier escalation path is live, but provider quality still differs materially:
  - local Ollama can sometimes satisfy simple feature specs
  - DeepSeek can participate but still frequently violates schema on training/model specs
  - OpenAI can produce correct structured specs when available, but if `OPENAI_API_KEY` is absent from the active shell/session it cannot serve as the repair tier
- After the latest train-override canonicalization pass, a model-route smoke workspace showed all three candidate artifacts (`feature_pack`, `train_override`, `generated_model`) succeeding on the local Ollama tier without needing OpenAI fallback.
- Local Ollama remains the least reliable tier operationally:
  - the new cooldown/healthcheck path prevents repeated long stalls, but it does not make the local daemon fast
  - if the Ollama daemon is up but overloaded, the system now fails fast and escalates instead of waiting out repeated long timeouts

## Documentation Maintenance Rule
- Stable truth belongs here.
- Historical entries belong in `CODEX_DEV_UPDATES.md`.
- Search/index shortcuts belong in `CODEX_DEV_LOG_INDEX.md`.
- Cross-AI active handoff belongs in `CLAUDE_CODEX_DIALOGUE.md`; closed or CDL-backed dialogue should be compacted into `CLAUDE_CODEX_DIALOGUE_ARCHIVE.md`.
- Every material change must refresh all three files in one turn.
- Each change entry must have:
  - entry id
  - local timestamp
  - type
  - scope
  - touched paths
  - summary
  - impact
  - validation
  - compatibility
  - rollback guidance when practical
