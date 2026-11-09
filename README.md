# 币安跟单榜监控台

「全部」tab 前 100 人，按跟单人数降序。PC / 手机自适应，点击带单员可在顶部切换
`仓位 / 平仓 / 交易` 三个 tab，并支持「仅公开持仓」筛选与分页。

数据源是币安网页端未文档化的 `bapi` 公开接口，纯标准库 Python，无 cookie、无登录。

## 本地运行

```bash
python3 trader_alert.py --once                     # 抓一轮，刷新 monitor-data.json
python3 trader_alert.py --web --watch 900         # 起网页 + 每 15 分钟刷新
# http://127.0.0.1:8777
```

`python3 build_static.py` 会生成完全自包含的 `docs/`，可直接双击 `docs/index.html` 或用任意静态托管打开。

## GitHub Actions 定时刷新

`.github/workflows/refresh.yml` 每半小时（`7,37 * * * *` UTC）跑一轮，产出快照、
推送增量事件、回写 state，并部署 GitHub Pages。

**先跑一次探测**，别直接开定时：

```
Actions -> 币安跟单榜刷新 -> Run workflow -> probe_only = true
```

### 关键限制：451

GitHub 托管 runner 的出口是美国 Azure IP，币安会返回
`451 Service unavailable from a restricted location`。workflow 因此把「探测」拆成
第一个 job，3 秒失败，不让 100 人的采集白跑。三种解法：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| 代理出口（推荐） | 仓库 Secrets 加 `BINANCE_PROXY=http://user:pass@host:port` | 需要一个非受限地区的 HTTP 代理；`urllib` 会自动读取 `HTTPS_PROXY`，脚本零改动 |
| 自建 runner | 把两个 job 的 `runs-on` 改成 `self-hosted`，跑在非美区 VPS 或本机 | 要维护一台常开的机器 |
| 不用 Actions | 本机 `launchd` 定时跑 `--once`，只把 `docs/` 推到 Pages | 电脑要常开，但没有 451 问题 |

`BINANCE_PROXY` 必须是 HTTP/HTTPS 代理（SOCKS5 原生 `urllib` 不支持）。
配好后重跑 `probe_only=true`，看到 `HTTP=200` 即可关掉探测开关正常跑。

### Secrets（按需，全部可选）

`WECOM_WEBHOOK` / `TG_BOT_TOKEN` + `TG_CHAT_ID` / `PUSH_WEBHOOK` —— 推送渠道。
不配则事件只写日志。`BINANCE_PROXY` —— 见上。

### 为什么 state 要提交回仓库

`trader_alert_state.json` 记录 `seen_positions`，用来只推「新增平仓」。Actions 每次
都是干净 runner，不持久化 state 的话每轮都会当成首次运行去建基线，永远不推送。
自动 commit 带 `[skip ci]`，也顺带避免 public 仓库 60 天无活动被停掉 schedule。

## 数据口径与延迟

- 榜单：`全部` tab + `30D` + `COPY_COUNT` 降序 + `hideFull=False`，前 100 人。
- ROI / PNL 每小时更新，Sharpe 12 小时，榜单本身慢变化。
- 订单流水延迟约 4 分钟，平仓记录数分钟到约 1 小时。**这是播报，不是成交信号。**
- 无公开 WebSocket 事件流，别把 cron 调到 5 分钟——不会更快，只会增加被风控概率。
- 定时任务在高峰期可能延迟 15–60 分钟，别依赖精确分钟。

## 额度

public 仓库的标准托管 runner 免费且不限分钟；private 仓库每月 2,000 分钟，
一轮约 5 分钟，半小时一次会超。本仓库建议保持 public（数据本身全是公开信息）。
