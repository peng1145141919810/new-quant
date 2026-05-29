# Project Law（H:\Ashare 版）

## 目的

这份文件定义本仓库的**硬规矩**。任何代码改动、AI 协作、工具调用都必须服从。

它不替代 STABLE 文档（事实快照）或 UPDATES 文档（改动流水）。它是更靠前的一层——决定**什么样的改动允许发生**。

## 工作区身份

- 本仓库：`H:\Ashare` —— **唯一的活跃工作区**
- 上游历史仓：`F:\quant_data\AshareC#` —— **只读，不写**
- 更早的活跃仓：`F:\quant_data\Ashare` —— **只读，不写**
- 默认运行：用户自己炒股，**目标是真正的全自动交易**，不是机构产品

## 四个目标（本仓库存在的全部理由）

所有改动必须能回答："这服务于以下哪个目标？"。如果一个都对不上，不要做。

1. **职能清理**：合并重叠的 supervisor / clock_supervisor / intelligent_scheduler / objective_scheduler / research_cycle_orchestrator / orchestrator_v6，规范模块边界。最终要能一句话说清"谁负责什么、谁向谁报告"。
2. **修 alpha**：当前每个 V5 cycle 都"无冠军"。根本原因疑似在研究方法（label 用绝对收益率没做截面排名/行业中性化、feature 自动收集所有数字列、事件窗口对 A 股太慢）。这是当前 PnL 瓶颈。
3. **LLM 规范化**：LLM 允许广泛参与（evidence audit、candidate review、structured spec codegen、overlay），但每一处使用必须**可解释、可关闭、可 A/B**。LLM 不能成为黑盒决策者。
4. **保留有效部分**：现有的票选机制在用户的非正式观察里有方向性正确。**不推倒重来**。任何模块要被砍之前，必须先证明它对最终选股没有正贡献。

## 硬规矩（违反就回滚）

### 关于代码量
1. **Net-zero rule**：任何新增模块 / gate / scheduler / 抽象层，必须先证明能删掉一个旧的。AI 协作者长期违反这条会把仓库再次养肥到尾大不掉。
2. **代码总量目标**：核心交易循环（信号→下单→监控）控制在 5000 行 Python 以内。研究侧不设硬上限但每月人工审一次有没有死代码。
3. **不为假设需求设计**：A 股个人账户不需要多机部署、不需要多账户路由、不需要多策略组合。三个相似的写法比一个抽象基类好。

### 关于运行
4. **不要默认跑全链路**：`launch_canonical.py` 不加 `--preflight-only` 就是真链路。除非用户明确授权当前会话可以跑长任务，否则只做文件检查 + 小探针 + `python -m py_compile`。
5. **不允许在 H 盘外写入 `F:\quant_data\`**：上游仓只读。
6. **不要修改 `local_settings.py` 里的 broker 解释器路径**：`gmtrade39` 是独立 Python，混了主 venv 会出问题。
7. **不要把 `quick_test` 当 smoke test 用**：它是真整合路径。

### 关于真钱
8. **接真钱前必须用户书面授权**：当前 broker 桥路径默认是 `precision`（掘金 paper），但物理上能通到真账户。任何启用真钱执行的改动必须在 CLAUDE_CODEX_DIALOGUE 里有用户明确同意的留言。
9. **release / OMS / execution 的 fail-closed 不能放宽**：宁可不动，不要乱动。

### 关于 LLM
10. **每个 LLM hook 必须有 enable 开关**：不能写死必走 LLM。
11. **LLM 输出必须有可解释痕迹**：写到 `llm_trace.py` 之类的审计文件，事后能复盘"为什么 LLM 这么说"。
12. **不允许 LLM 直接修改最终排序权重**：LLM 只能 boost/penalize，不能替代量化分数。

### 关于文档
13. **任何行为/接口/数据路径改动必须同步更新**：`CODEX_DEV_STABLE.md` + `CODEX_DEV_UPDATES.md` + `CODEX_DEV_LOG_INDEX.md`。漏改一个就算这次改动未完成。
14. **跨 AI 的争议、质疑、移交必须写进 `CLAUDE_CODEX_DIALOGUE.md`**：不要靠用户传话。
15. **任何新增 markdown 文档要写明：用途、维护者、过期条件**。否则会再次出现一堆 `*_CN.md` 文档堆积无人打理。

## 目录状态标记

- `live`：当前运行时和当前业务输出
- `pending-cleanup`：已知有问题但还没修，列入下一轮工作清单
- `experiment`：探针 / 临时输出 / 生成的候选实验。**不是 canonical truth**
- `deprecated`：保留只为过渡期，必须指明替代品

## 当前分类

- `live`
  - `launch_canonical.py`、`main_research_runner.py`、`trade_clock_service.py`
  - `src/ashare/`
  - `data/sql_store/`、`data/daily_csv_qfq/`、`data/enriched_daily_csv_qfq/`、`data/event_lake_v6/`、`data/affordable_feeds/`、`data/auxiliary_data/`、`data/ml_datasets/`
  - 所有 Codex 三件套 + `CLAUDE_CODEX_DIALOGUE.md` + 本文件
- `pending-cleanup`
  - 6 个 supervisor/scheduler 互相争 authority（目标 #1）
  - `dataset.py` 的 label / feature 自动收集逻辑（目标 #2）
  - `strategy_activation.py` 的事件窗口长度（目标 #2）
  - 散落各处的 `F:\quant_data\AshareC#` 硬编码路径
  - `local_settings.py` 的 F 盘 venv 路径
- `experiment`
  - `data/ml_datasets/train_table_v1/`（V5 训练样本，不是 canonical truth）
- `deprecated`
  - 任何引用 `csharp_runtime_skeleton` / `python_rpc_bridge` / `site_portal` / `ashare_control` / `operator_chat_backend` / `portal_backend` 的代码——这些后端已删，相关引用要清理后删除

## 非目标（明确不做）

- 不做团队协作友好度优化（单人项目）
- 不做多机器部署 / Docker / k8s
- 不做 C# 业务迁移
- 不做实时操作员网页
- 不做远程 ollama 隧道
- 不重新引入 C#↔Python RPC 桥
- 不为「将来可能需要」做抽象
