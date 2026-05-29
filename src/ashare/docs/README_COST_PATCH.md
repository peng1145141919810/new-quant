# 省钱版与持仓建议版说明

本版做了三件核心改动：
1. V6 事件抽取改为“规则粗筛 + 硬限流 + DeepSeek 批处理 + GPT-5.4 一次性出 brief”。
2. integrated_supervisor 默认先跑 V6 一轮，再跑 V5.1 GPU 多轮实验，再自动生成持仓建议文件。
3. 新增持仓建议输出：target_positions.csv、rebalance_orders.csv、portfolio_recommendation.json。

最重要的手改入口仍然只有：
- `engine/local_settings.py`
- 根入口：`run_v6_full_cycle_real.py`
