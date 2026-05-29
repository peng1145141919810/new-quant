下面是本轮研究证据包。请输出一份 research_brief.json。

## 证据包
{{research_context_pack_json}}

## 输出重点
1. `research_thesis` 用一句话总结今天研究主线，`core_theses` 再展开成 3~5 条主命题
2. 哪些现有数据必须补
3. 哪些特征应重算
4. 哪些新特征值得进候选实验
5. 哪些 route 应增配，哪些应减配
6. 哪些 branch 应暂停
7. 给出 5~12 个 `candidate_experiments`

## 结构要求
- 全部字段尽量短句输出，避免长段落；单个字符串优先控制在 60 个中文字符以内
- `priority_events` 只保留真正驱动今天研究方向的事件
- `core_theses` 每项都要写清楚 `hypothesis`、`why_now`、`required_features`、`target_labels`、`route_bias`
- `candidate_experiments` 每项都要写清楚 `route`、`features`、`models`、`labels`、`top_k`、`reason`
- `candidate_experiments` 优先输出 6 个，`features` 不超过 4 个，`models` 不超过 2 个，`labels` 不超过 2 个
- `data_actions` / `feature_actions` / `label_actions` / `model_actions` / `portfolio_actions` / `risk_actions` 都必须是可执行动作，不允许空话
