# 掘金模拟盘组合调仓桥接

## 这包干什么

它把研究系统生成的 `latest_portfolio_v1.csv` 接到掘金仿真账户：

- 读取最新持仓建议
- 识别证券代码列和权重列
- 读取价格
- 查询掘金仿真当前资金和持仓
- 生成调仓单
- 发送委托到掘金仿真
- 写出 orders / fills / raw / execution_report

## 价格从哪来

优先级如下：

1. 如果 `price_snapshot_path` 指向的 CSV 存在，就优先用这个文件里的价格；
2. 如果 `latest_portfolio_v1.csv` 自己带了 `price / close / last_price / last / adj_close / open` 其中任一列，就直接拿来用；
3. 当前账户已有老仓，会用掘金返回的持仓价格补齐，避免老仓因为无价不能清掉。

如果新目标证券既不在外部价格快照里，也不在组合文件价格列里，脚本会直接报错，不会瞎下单。

## 建议的第一次运行方式

第一次先用一份手工控制的小组合文件，不要直接拿全量研究结果猛冲。

例如只放 2~3 只股票，总权重 30%~50%。确认委托逻辑和持仓回写没问题，再切回正式 `latest_portfolio_v1.csv`。

## 运行命令

```powershell
F:\quant_data\Ashare\venvs\gmtrade39\Scripts\python.exe run_gmtrade_portfolio_bridge.py --config config\gmtrade_runtime_config.local.json
```

## 输出文件

- `orders_时间戳.csv`：本次计划委托
- `fills_时间戳.csv`：本次识别到的成交回报
- `gmtrade_raw_时间戳.csv`：原始委托/回报摘要
- `latest_target_snapshot.csv`：本次目标仓位快照
- `latest_account_state.json`：当前账户状态
- `equity_curve.csv`：净值时间序列
- `execution_report_时间戳.json`：本次执行摘要
