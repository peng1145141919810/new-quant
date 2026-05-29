# V6 可运行版补丁说明

这份包已经把以下东西改好了：
- OpenAI 研究脑改成 Responses API。
- DeepSeek 低成本执行脑改成官方兼容写法。
- 主配置文件改成 `configs/hub_config.v6.autorun.json`，不需要你再手改。
- 运行模式默认是 `full_cycle`。
- 真实接入 Tushare 抓公告、新闻与基础行情缓存。

## 你现在只需要做两件事
1. 安装依赖：
```powershell
pip install -r requirements_v6_runtime.txt
```
2. 运行：
```powershell
.\scripts\run_v6_full_cycle_real.ps1 -TushareToken "你的 Tushare Token"
```

DeepSeek 和 OpenAI 的 key 继续走你现有的 Windows 环境变量，不需要改这个包里的任何配置文件。
