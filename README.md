# Ashare 量化工作区（H:\Ashare）

A 股个人全自动量化系统。Python 单体。

## 这是什么仓库

这是 `F:\quant_data\AshareC#` 在 2026-05-29 fork 出来的精简副本，**砍掉了所有非核心部分**：

- 砍掉了 `csharp_runtime_skeleton/`（伪迁移的 C# 治理壳）
- 砍掉了 `python_rpc_bridge.py`（C#↔Python 桥）
- 砍掉了 `site_portal/`、`ashare_control/`、`operator_chat_backend/`、`portal_backend/`（网页和操作员控制台）
- 砍掉了所有 `deploy_*` / `install_*_autostart` / `publish_*` / `*_to_gdrive*` / `start/stop_codex_dev_log_sync*` / `*_operator_ollama_*` 脚本
- 砍掉了 124 GB 的 GPU 训练残留 (`data/research_hub_v5_1_gpu_integrated/`)
- 砍掉了所有过期运行时状态（`trade_release_v1`、`live_execution_bridge`、`trade_clock`、`research_hub_integrated` 等）
- 砍掉了 venv（重装即可）和 .git（干净起步）

**留下的就是真正在运行的东西**：Python 业务核心、SQL 数据库、价格 CSV、训练样本、治理文档。

总大小 ~21.6 GB。

## 入口

| 用途 | 文件 |
|---|---|
| 正式运行 | `launch_canonical.py` |
| 业务根 | `main_research_runner.py` |
| 交易时钟 | `trade_clock_service.py` |
| 运行时根 | `src/ashare/` |
| 数据 | `data/` |

## 入仓必读顺序

1. `CODEX_DEV_LOG.md`（landing page）
2. `CODEX_DEV_STABLE.md`（当前事实）
3. `CODEX_DEV_LOG_INDEX.md`（检索）
4. `CODEX_DEV_UPDATES.md`（历史改动）
5. `CLAUDE_CODEX_DIALOGUE.md`（Claude 和 Codex 互相留言的地方）
6. `PROJECT_LAW.md`（治理硬规矩）
7. `AGENTS.md`（AI 协作行为规范）

## 当前已知的"必须先修才能跑"

H 盘的代码**还没在 H 盘上跑过一次**。在第一次跑之前要：

1. **重装 venv**，按 `src/ashare/requirements_v6_runtime.txt`
2. **改 `src/ashare/engine/local_settings.py`** 里的 Python 路径（旧的指向 F 盘）
3. **清理硬编码 `F:\quant_data\AshareC#\` 路径引用**——源码和 configs 里散落很多
4. **决定 `D:\AshareHotData\research_hub_integrated\runs` 这个 NVMe junction 怎么处理**——要么重建要么改走 H 盘
5. **先 `--preflight-only` 跑一遍**确认能起来再说

详见 `CODEX_DEV_STABLE.md` 的「Pending H: Workspace Setup」一节。

## 不再做的事

- ❌ C# 实现策略（伪迁移结束）
- ❌ 操作员网页 / 远程控制台
- ❌ 跨语言 RPC 桥
- ❌ Google Drive 自动同步
- ❌ 远程 ollama 反向隧道
- ❌ 任何"先全跑再说"的端到端默认运行

## 四个目标（本仓库存在的全部理由）

1. **职能清理**：合并重叠的 supervisor/scheduler，规范模块边界
2. **修 alpha**：label / feature / 事件窗口的研究方法重做
3. **LLM 规范化**：广泛使用但保留可解释性
4. **保留有效部分**：不推倒重来，已经有效的票选机制要留

详见 `PROJECT_LAW.md`。

## 给 AI 协作者的话

如果你是 Claude 或 Codex，请在写代码前：

1. 通读上面的「入仓必读顺序」
2. 检查 `CLAUDE_CODEX_DIALOGUE.md` 是否有对方留给你的未处理留言
3. 任何代码变更必须同步更新 STABLE/UPDATES/INDEX 三件套
4. **不要加新的 scheduler / gate / 抽象层**。任何新增都必须先证明能删掉一个旧的（net-zero rule）。
