# Research Brain

This directory is the embedded V5.1 research runtime used by the current Python system. It is not the old root-level archived `quant_research_hub_v5*` copy.

## Role In The Current System
- supplies deeper research cycles behind the active runtime
- is invoked through the Python runtime chain, not as the formal workspace entry
- remains part of the hybrid system even though top-level governance now starts from `launch_canonical.py`

## Important Boundary
- Do not treat this directory as the workspace root.
- Do not bypass the canonical launcher flow unless the task explicitly requires research-brain-only inspection.

## Read Next
- `..\..\..\CODEX_DEV_STABLE.md`
- `..\..\..\RUN_PROFILES.yaml`
