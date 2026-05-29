这是一份重新收拢后的 V6 整包。

本次重打重点：
1. 事件抽取改为：规则粗筛 + 本地 Ollama 优先 + 规则兜底。
2. 研究脑喂入的证据包改为超短证据卡。
3. GPT 研究计划不再走 strict schema，避免 schema 导致整轮 fallback。
4. 保留 orchestrator 所需兼容接口：
   - extract_events_with_worker
   - save_event_store
   - build_research_brief
   - save_research_brief

你需要确认：
1. 本地 Ollama 服务已经启动。
2. local_settings.py 里的路径仍然符合你本机。
3. 如果不想被 24 小时闸门跳过，删除 research 目录下的 last_token_plan.json 或把 TOKEN_PLAN_MIN_INTERVAL_HOURS 改为 0。
