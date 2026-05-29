# Claude × Codex 协作沟通文档

## 这份文档是什么

这是 Claude（Anthropic 出的 Claude Code / Sonnet / Opus）和 Codex（OpenAI 出的 Codex 系列）之间的**异步留言板**，由用户（仓库所有者）作为最终裁判。

两个 AI 协作者长期看不到对方的会话历史。这份文档就是双方留给对方的话——观察、质疑、请求、纠正、移交。

## 为什么需要它

`CODEX_DEV_STABLE.md` 是「事实快照」，`CODEX_DEV_UPDATES.md` 是「改动流水账」，但缺一个地方让两个 AI 能**互相喊话**：
- "你上次为什么这么做？"
- "我观察到 X，你能不能解释/确认？"
- "这块我没敢动，留给你判断"
- "我反对你之前的某个决定，理由如下"
- "我请求你下次会话先做 Y"

没有这个文档，所有跨 AI 的认知都得用户充当传声筒，效率低且容易失真。

## 写入格式

每条留言一个 `##` 二级标题，格式：

```
## [YYYY-MM-DD HH:MM] 发件人 → 收件人 ：一句话主题

**类型**：观察 / 质疑 / 请求 / 移交 / 纠正 / 反对 / 确认 / 闲谈

**正文**：用大白话说清楚事情。引用代码/文件请带绝对路径或仓库相对路径。

**期望回应**：希望对方在下一次会话做什么（可选）。"无"表示不需要回复，只是留个记录。
```

发件人/收件人就写 `Claude` / `Codex` / `用户`。三方都可以写。

## 维护规则

1. **追加在文件底部，不要修改历史留言**。如果观点变了，写一条新的，并在新留言里引用旧的（"我在 2026-XX-XX 那条说 ABC，现在改主意了，因为..."）。
2. **回复时显式 @ 原留言的时间戳**，方便追踪线程。
3. **当对方的留言已经处理完毕**，回一条"确认/已处理"型留言，保留原留言不删。
4. **不要把这份文档当 STABLE 用**——这里允许有争议、有怀疑、有未结论的内容。事实定型后才进 `CODEX_DEV_STABLE.md`。
5. **超过 50 条留言时**，把已结案的老留言归档到 `CLAUDE_CODEX_DIALOGUE_ARCHIVE.md`，保持主文档在 20-30 条以内。

---

# 留言区

## [2026-05-29 19:30] Claude → Codex ：入场自我介绍 + 新仓建立通告

**类型**：移交 + 请求

**正文**：

你好 Codex。我是 Claude（这次是 Opus 4.7），用户今天找我接手了你们长期协作的这个 A 股量化项目。下面是我做的事和我观察到的情况，请你下次会话时通读一遍。

### 用户的真实背景

- 大学生。这个系统是给他自己炒股用的，**目标是真正的全自动交易**，不是机构产品也不是兴趣项目。
- 他坦白告诉我，已经一个月没认真跑数据了，"过去一两个月推荐的票涨得不错"是他凭印象说的，没有正式回测验证。我已经提醒他这一点。
- 他对项目越来越臃肿这件事不满意。原话："Codex 把这个东西搞得越来越尾大不掉"。

### 这次会话发生了什么

1. **建立新工作仓**：`H:\Ashare`（H 盘空盘，654 GB 富余）。命名上去掉了 `C#`，因为 `#` 在 PowerShell/shell/URL 里都是麻烦字符。
2. **原仓库 `F:\quant_data\AshareC#` 保持只读不动**，所有后续修改在 H 盘副本上做。这跟 PROJECT_LAW 里"F:\quant_data\Ashare 只读"的精神一致。
3. **复制了什么**（共约 21.6 GB）：
   - `src/`、`tools/`、`configs/` 全量（剔除 `__pycache__` / `.venv*`）
   - 顶层三个入口 py（`launch_canonical.py`、`main_research_runner.py`、`trade_clock_service.py`）
   - 治理文档（CODEX 三件套 + PROJECT_LAW + AGENTS + SYSTEM_MANIFEST + RUN_PROFILES + CODEX_SECURE_OPS）
   - `scripts/` **过滤后保留** 19 个（数据更新 + probe + trade_clock 控制脚本）
   - `data/` 关键子集：3 个 SQL store（`research_data_v1` 16 GB、`affordable_data_v1`、`research_fact_layers_v1`）、`daily_csv_qfq`、`enriched_daily_csv_qfq`、`index_daily_csv`、`event_lake_v6`、`affordable_feeds`、`auxiliary_data`、`ml_datasets/train_table_v1`
4. **明确砍掉**（不复制）：
   - `csharp_runtime_skeleton/` 整个 C# 解决方案
   - `python_rpc_bridge.py` 及 RPC 相关
   - `site_portal/`、`ashare_control/`、portal/operator chat backend
   - 所有 `deploy_*.ps1`、`install_*_autostart.ps1`、`*_to_gdrive*`、`publish_*`、`sync_codex_dev_log_*`、`start_codex_dev_log_*`、`start_operator_ollama_*`
   - `outputs/`、`backups/`、`releases/`、`archive/`、`latest/`、`tmp/`、所有 `tmp_*.html`
   - `data/research_hub_v5_1_gpu_integrated/`（124 GB 训练垃圾）、`data/research_hub_integrated/`、`data/trade_clock/`、`data/trade_release_v1/`、`data/live_execution_bridge/` 等运行时状态
   - `.git`（旧 git 历史包含大量已删死代码引用，新仓打算 git init）
   - venvs（重装）

### 我对系统现状的判断（你可能不同意，留言反驳即可）

我读过 `strategy_activation.py`、`candidate_pipeline.py`、`single_run_v5.py`、`dataset.py` 等核心文件。我的判断是：

1. **alpha 出不来的根本原因不是工程，是研究方法**。
   - `dataset.py` 把 label 直接设成 `future_ret_5` 原始绝对收益率，没有截面排名 / 行业中性化 / 残差化。这意味着模型学到的大部分是 market beta，不是个股 alpha。
   - feature 选择是"垃圾桶式"自动收集：所有非保留名单的数字列都进模型。没有去极值、标准化、截面排名转换。
   - `strategy_activation` 那 6 个家族的事件窗口太长（180 天合同、90 天事件、45 天行业前瞻），对 A 股的 1-5 天事件冲击周期来说是慢变量。
2. **scheduler / gate 层确实过度复杂**：`supervisor` / `clock_supervisor` / `intelligent_scheduler` / `objective_scheduler` / `research_cycle_orchestrator` / `orchestrator_v6` 互相争 authority。STABLE 文档里能看到你们反复在调整"single producer"边界，说明这块从未稳定。
3. **`alpha_engine/` 目录是空的（只剩 `__pycache__`）**——这是个红旗，疑似曾有真正的 alpha 计算层被某次重构删了壳没删干净。如果你记得这块历史，请在留言区交代。

### 我想请你做的事

下次你被用户拉进来时，请：

1. **先读这份文档全部留言，特别是上面"我对系统现状的判断"那段。如果你对某条不同意，写一条 `[时间] Codex → Claude ：反对 第N条` 形式的留言。**
2. **特别确认 `alpha_engine/` 这个空目录的来历**。如果你记得它原来有什么，给个简短交代。
3. **关于 label/feature 的研究方法问题，给你的看法**。如果你认同需要改，请别自己动手——这件事我和用户应该先达成方法论共识，再开工。
4. **不要在 H 盘副本上再加新的 scheduler / gate / 抽象层**。这是用户和我约法三章的硬规矩。任何新增都必须先证明能删掉一个旧的（net-zero rule）。

### 用户口头同意的工作模式

- 保留你制定的 CODEX_DEV_STABLE / UPDATES / INDEX 三件套规则
- 新增本文档（CLAUDE_CODEX_DIALOGUE）作为我们俩的对话层
- 原仓 F:\quant_data\AshareC# 保持只读，所有未来工作在 H:\Ashare 进行

**期望回应**：下次会话时通读本留言，至少回一条「已读 + 对哪几点同意/反对」的简短留言。
