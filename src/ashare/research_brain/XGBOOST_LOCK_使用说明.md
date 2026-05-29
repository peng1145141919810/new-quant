# XGBoost 锁定版使用说明

1. 覆盖你本地的 `quant_research_hub_v5_1`，或者单独解压为新目录。
2. 先在 `hub/local_settings.py` 里设置：
   - `MODE = "validate_only"`
   - `DRY_RUN = True`
3. 运行 `run_research_hub_v5_1_local.py`
4. 看 `validation.json` 里是否出现并通过 `xgboost_gpu_fit`
5. 再改成：
   - `MODE = "batch"`
   - `DRY_RUN = False`
6. 后续常驻再用 `adaptive_research_brain`

这版已经把 GPU 主路线固定为 `xgboost_gpu`，不再纠结 LightGBM GPU。
