"""H:\\Ashare 工作区的本地配置 loader.

行为：
1. 先从 `local_settings.example.py` 加载所有大写常量作为基线
2. 然后对 H 盘环境做必要的覆盖（venv 路径、被砍模块的开关）
3. Secrets（TUSHARE_TOKEN / OPENAI_API_KEY / DEEPSEEK_API_KEY）仍然走用户环境变量，
   不在文件里存任何 token 值

旧的「从 F:\\quant_data\\Ashare 的 legacy local_settings 拉 overlay」的链路已删除：
该路径在本机上实际不存在，是 dead code。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any


_HERE = Path(__file__).resolve().parent
_EXAMPLE_PATH = _HERE / "local_settings.example.py"


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_upper_setting(name: str, value: Any) -> bool:
    return name.isupper() and not callable(value)


# 1. 先加载 example 作为基线
_example = _load_module(_EXAMPLE_PATH, "engine._local_settings_example")
for _name in dir(_example):
    _value = getattr(_example, _name)
    if _is_upper_setting(_name, _value):
        globals()[_name] = _value


# 2. H 盘工作区专属覆盖

# 主研究 venv（example 里的逻辑已经会自动找 REPO_ROOT/.venv313/Scripts/python.exe，
# REPO_ROOT 已经是 H:\Ashare，所以默认行为就对。这里只是兜底显式声明。）
_VENV313_PY = Path(r"H:\Ashare\.venv313\Scripts\python.exe")
if _VENV313_PY.exists():
    globals()["PYTHON_EXECUTABLE"] = os.environ.get("ASHARE_RESEARCH_PYTHON", str(_VENV313_PY))

# Broker 桥用独立的 Python 3.9 venv（与主研究栈完全隔离）
_GMTRADE_PY = Path(r"H:\Ashare\.venv\gmtrade39\Scripts\python.exe")
if _GMTRADE_PY.exists():
    globals()["GMTRADE_PYTHON_EXECUTABLE"] = str(_GMTRADE_PY)


# 3. 被砍模块的硬性禁用——避免代码路径走到不存在的东西

# 站点发布已经被砍（site_portal/operator_chat_backend/portal_backend 都不在 H 盘）
globals()["ENABLE_AUDIT_SITE_PUBLISH"] = False
globals()["AUDIT_SITE_PUBLISH_RUN_AFTER_SUMMARY"] = False

# csharp_runtime_skeleton 不存在；hot-reload 不要扫这个根
globals()["TRADE_CLOCK_RUNTIME_HOT_RELOAD_ENABLED"] = False

# 小账户改造：单遍 decision engine 已统一管控持仓数与集中度，
# 关闭下游 portfolio_control 的“强制铺成 N 只种子仓”逻辑，避免把集中组合摊散。
globals()["PORTFOLIO_CONTROL_BOOTSTRAP_DIVERSIFICATION_ENABLED"] = False
globals()["PORTFOLIO_CONTROL_BOOTSTRAP_MIN_NAMES"] = 3
# decision engine 单名/持仓数约束（micro 1-2万账户：单只≤25%、3-5只、panic只减不加）
globals()["DECISION_ENGINE_MAX_NAMES"] = 5
globals()["DECISION_ENGINE_MIN_NAMES"] = 3
globals()["DECISION_ENGINE_SINGLE_NAME_CAP"] = 0.25
globals()["DECISION_ENGINE_PANIC_ONLY_REDUCE"] = True

# 远程 trade-clock delegate（SSH 到 43.129.28.141）默认关掉，
# 本地全自动是 H 盘的目标，不依赖远端 worker
globals()["ENABLE_TRADE_CLOCK_REMOTE_DELEGATE"] = False


# 4. PROJECT_ROOT 兜底（部分老代码引用）
if "PROJECT_ROOT" not in globals():
    globals()["PROJECT_ROOT"] = globals().get("PACKAGE_ROOT", _HERE.parent)
