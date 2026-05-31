# -*- coding: utf-8 -*-
from __future__ import annotations
from pathlib import Path
from engine.config_builder import save_runtime_config
from engine import local_settings as LS
from engine.orchestrator import run_cycle
from engine.supervisor import run_integrated_supervisor

if __name__ == '__main__':
    package_root = Path(__file__).resolve().parent
    config_path = save_runtime_config(package_root / 'configs' / 'hub_config.local.json')
    print('===== 研究计划 START =====')
    print('配置文件:', config_path)
    print('运行模式:', LS.RUN_MODE)
    if LS.RUN_MODE == 'integrated_supervisor':
        run_integrated_supervisor(config_path)
    else:
        run_cycle(config_path=config_path, mode=LS.RUN_MODE)
    print('===== 研究计划 DONE =====')
