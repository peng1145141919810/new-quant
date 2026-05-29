# Deprecated Entrypoints

The following operator habit is deprecated:

- running `run_v6_full_cycle_real.py` as if it were the current formal entry

Current replacements:
- Formal operator entry:
  - `F:\quant_data\Ashare\launch_canonical.py`
- Wrapped business root entry:
  - `F:\quant_data\Ashare\main_research_runner.py`

Operator rule:
- Use `launch_canonical.py` for formal runs.
- Inspect `main_research_runner.py` when you need the direct business chain.
