# 接入掘金量化模拟盘：第一步

## 这包里有什么

- `step1_gmtrade_smoke_test.py`：只做登录 + 查资金 + 查持仓。
- `step2_gmtrade_order_probe.py`：打一笔极小额限价单，确认委托链路通。
- `gmtrade_local_config.example.json`：配置模板。

## 你要怎么放

建议放到你的工程根目录：

```text
F:\quant_data\Ashare\quant_research_hub_v6_cost_control_portfolio_integrated_full\quant_research_hub_v6_lean_portfolio_integrated_full\live_execution_bridge\gmtrade_sim\
```

## 你要做什么

1. 把 `gmtrade_local_config.example.json` 复制一份，改名成 `gmtrade_local_config.json`
2. 填写：
   - `token`
   - `account_id`
3. 在你的虚拟环境里安装：
   - `pip install gmtrade`
4. 先运行 `step1_gmtrade_smoke_test.py`
5. 成功后再运行 `step2_gmtrade_order_probe.py`

## 注意

- 第二个脚本会真的往仿真账户发一笔委托。
- 默认示例是 `SHSE.600000` 买 100 股，限价 10 元，只是示意，你要自己改成合适价格。
- 只要这两步打通，后面就能把你的持仓建议执行层正式接进去。
