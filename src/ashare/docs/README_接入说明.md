# live_execution_bridge 执行层接入包

## 这包是干什么的

这不是新的研究脑，也不是新的回测器。
它是把你现有的 **5.1 / V6 持仓建议** 接到 **模拟交易 / 后续真实交易软件** 的执行层桥接包。

第一版目标只有一个：

- 把 `latest_portfolio_v1.csv` 或同类持仓建议文件
- 转成目标仓位
- 对比当前持仓
- 生成调仓单
- 先在本地模拟账户里执行
- 产出可复盘的订单、成交、持仓、权益曲线快照

## 推荐放置位置

把整个文件夹放到你现在较稳定的工程根目录下：

```text
F:\quant_data\Ashare\quant_research_hub_v6_cost_control_portfolio_integrated_full\quant_research_hub_v6_lean_portfolio_integrated_full\live_execution_bridge_package_20260320
```

更建议你实际落地时，把里面的 `live_execution_bridge` 目录和 `run_daily_paper_bridge.py` 复制到：

```text
F:\quant_data\Ashare\quant_research_hub_v6_cost_control_portfolio_integrated_full\quant_research_hub_v6_lean_portfolio_integrated_full\live_execution_bridge
```

## 运行前要改哪里

### 1）先复制配置文件

把：

```text
config\runtime_config.example.json
```

复制为：

```text
config\runtime_config.local.json
```

### 2）重点修改这些路径

- `portfolio_root`
  - 改成：`F:\quant_data\Ashare\data\research_hub_v5_1_gpu_integrated`
- `portfolio_recommendation_root`
  - 改成：`F:\quant_data\Ashare\data\portfolio_recommendation_v6`
- `output_dir`
  - 建议改成：`F:\quant_data\Ashare\data\live_execution_bridge`
- `price_snapshot_path`
  - 改成你当日价格快照 CSV 的路径
- `initial_cash`
  - 改成你希望模拟盘起始资金

## 第一版运行方式

```bash
C:\Users\Administrator\PyCharmMiscProject\.venv\Scripts\python.exe run_daily_paper_bridge.py --config config\runtime_config.local.json
```

## 这版会输出什么

在 `output_dir` 下会生成：

- `account_state.json`：账户状态
- `orders_YYYYMMDD_HHMMSS.csv`：本轮订单计划
- `fills_YYYYMMDD_HHMMSS.csv`：本轮模拟成交
- `execution_report_YYYYMMDD_HHMMSS.json`：本轮执行总结
- `equity_curve.csv`：账户净值轨迹
- `latest_target_snapshot.csv`：本轮目标仓位快照

## 现在支持什么

### 已经能跑的

- 本地模拟账户
- 自动读取最新持仓建议文件
- 自动识别常见证券代码列、权重列
- 按 A 股 100 股整数手调仓
- 卖出优先，再买入
- 手续费、滑点、最小成交金额限制
- 输出完整复盘材料

### 目前只留了接口、还没实接的

- `xtquant_adapter_stub.py`
- `gmtrade_adapter_stub.py`
- `futu_adapter_stub.py`

原因很简单：
我现在能对接口方向给你非常明确的结构，但没在你的本机终端环境里做联调，不该假装这些适配器已经百分之百能跑。
所以我这里故意不装神弄鬼，只把 **稳定能跑的本地模拟层** 做实，把 **交易软件适配层** 预留清楚。

## 我对你下一步的建议

### 最务实的落地顺序

1. 先把这包接进你现在稳定的量化工程。
2. 先让它吃到你真实跑出来的 `latest_portfolio_v1.csv`。
3. 先连续跑 5~10 个交易日的“准实盘模拟”。
4. 看订单、成交、换手、持仓漂移、净值曲线有没有明显问题。
5. 执行层稳定后，再选一个外部交易软件做真正的 API 对接。

### 软件路线怎么选

- **如果你要最快把模拟盘跑起来**：优先考虑掘金仿真或富途 A 股模拟。
- **如果你最终目标是 A 股实盘自动化**：结构上要优先兼容 QMT / MiniQMT 这条路。

原因不是情怀，是现实：
你的系统最终想落到 A 股真实下单，执行层不能一开始就绑死在只会模拟、不会转实盘的环境里。

## 注意

这包第一版默认是 **日频调仓执行层**，不是分时高频系统。
这不是退缩，而是对你现在的研究系统阶段最合适：
你当前的研究与持仓建议本来就是日频产出，硬上分时只会把系统复杂度炸掉。
