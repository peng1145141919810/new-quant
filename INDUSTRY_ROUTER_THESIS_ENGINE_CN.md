# Industry Router Thesis Engine 使用说明

## 1. 这次改造解决什么问题

新的 `industry_router` 不再把 `structured_events` 当主对象，也不再让消息面直接决定 `allow_entry`。

现在正式的收益归因链是：

1. `theme_registry.seed.csv` 定义主题宇宙
2. `company_exposure_map.seed.csv` 定义公司如何受益
3. `strategy_spec.json` 定义冲击类型、评分规则和准入阈值
4. 运行时把 `official source / price context / event clue` 整理成 `EvidenceBundle`
5. 由 `IndustryThesis` 打分
6. 再把 thesis 分数投射到股票层，生成 `latest_stock_signal.csv`

所以现在问“收益来自哪里”，答案必须回到：

- 哪个 `theme_id`
- 哪个 `thesis_id`
- 哪类 `shock_type`
- 哪些 `EvidenceItem`
- 哪条 `profit_path`

## 2. 正式入口

仓库外壳入口不变，仍然是：

```powershell
python F:\quant_data\AshareC#\launch_canonical.py --mode industry_router_only --profile quick_test
```

如果只想在包内直接调用，公开入口仍然是：

```python
from engine.industry_router import build_industry_router_artifacts
```

## 3. 正式 contract

这次重构后，`industry_router` 的正式研究层 contract 是：

- `quant_research_hub_v6_repacked_clean\quant_research_hub_v6_repacked_clean\configs\industry_router\theme_registry.seed.csv`
- `quant_research_hub_v6_repacked_clean\quant_research_hub_v6_repacked_clean\configs\industry_router\company_exposure_map.seed.csv`
- `quant_research_hub_v6_repacked_clean\quant_research_hub_v6_repacked_clean\configs\industry_router\strategy_spec.json`
- `quant_research_hub_v6_repacked_clean\quant_research_hub_v6_repacked_clean\configs\industry_router\source_contracts.json`

其中：

- `theme_registry.seed.csv` 决定允许研究哪些主题
- `company_exposure_map.seed.csv` 决定股票为什么受益
- `strategy_spec.json` 决定 thesis 如何打分、什么时候允许准入
- `source_contracts.json` 决定官方证据源

## 4. 运行时数据流

新的内部链路是：

1. `core/loaders.py`
   - 读取 contract 和静态基础表
2. `registry/theme_registry.py`
   - 构建 theme 和 company exposure runtime registry
3. `evidence/builders.py`
   - 把官方源、价格上下文、事件线索整理成 `EvidenceBundle`
4. `scoring/engine.py`
   - 生成可审计的 `ThesisScoreCard`
5. `thesis/engine.py`
   - 构建 `IndustryThesis` 和 `StockSignal`
6. `outputs/writers.py`
   - 写出 summary、context、daily csv

## 5. 关键约束

### 5.1 消息降级

`event_clue` 现在只能作为线索：

- 可以提示“值得怀疑”
- 可以提高 thesis 关注度
- 不能单独把 `allow_entry` 打开

如果没有真实非事件证据，thesis 会被打成 `blocked` 或 `watch`。

### 5.2 股票分数的来源

股票层 `final_score` 现在来自六个明确维度：

- `evidence_score`
- `causal_clarity_score`
- `persistence_score`
- `exposure_score`
- `underpricing_score`
- `crowding_penalty`

### 5.3 market_state 的来源

`market_state` 不再读取消息分行业计数。
现在它从 `latest_stock_signal.csv` 中的 thesis 分数按 `mechanism_primary` 聚合出 `mechanism_scores`。

## 6. 主要产物

核心产物位置仍在 `research_root/industry_router` 下，重点看：

- `latest_stock_signal.csv`
- `stock_signal_daily.csv`
- `industry_router_summary.json`
- `thesis_daily.csv`
- `theme_evidence_daily.csv`
- `event_clue_daily.csv`
- `mechanism_state_daily.csv`

## 7. 怎么审计一条信号

看一只股票为什么被推荐，顺序是：

1. 在 `latest_stock_signal.csv` 找 `ts_code`
2. 看它的 `theme_id / thesis_id / shock_type / profit_path`
3. 去 `thesis_daily.csv` 看同一个 `thesis_id`
4. 去 `theme_evidence_daily.csv` 看这个 `theme_id` 的证据链
5. 去 `industry_router_summary.json` 看 `active_theses` 和 `evidence_overview`

## 8. 当前已删掉的旧核心

这次已经移除：

- 旧 `mechanisms/` policy 树
- 旧 `event_pipeline.py`
- 旧 `signal_loader.py`
- 旧 `backtest_engine.py`
- 旧 `backtest.py`

也就是说，仓库里不再有一套正式可运行的 event-centric router 核心。
