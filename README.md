# 币安跟单带单员监控台

自选带单员 → 一人一个 ntfy 主题 → 开平仓与订单推送到手机，网页版随时看当前仓位。

线上只读站点：https://dzp181.github.io/binance-copytrade-monitor/

## 它是什么

不是「跟单榜前 100 人」，而是**你点名要盯的几个人**：

- 搜昵称或粘贴带单员主页链接加入名单（`watchlist.json`）
- 每人一个独立推送主题 `bn-<pid 后 8 位>`，订阅哪个就只收哪个
- 每 30 分钟抓一次：当前持仓、最近平仓、最新订单
- 一轮内同一个人的多笔平仓合并成一条通知，不刷屏

## 快速开始（本机，全功能）

```bash
python3 trader_alert.py --web --watch 900     # 网页 + 后台采集，打开 http://127.0.0.1:8777
python3 trader_alert.py --search 星辰          # 命令行搜人
python3 trader_alert.py --add 4788776444236355328
python3 trader_alert.py --add "https://www.binance.com/zh-CN/copy-trading/lead-details/4794150057005208320"
python3 trader_alert.py --list
python3 trader_alert.py --remove 4788776444236355328
python3 trader_alert.py --test-ntfy           # 推送自检
```

只有本机跑 `--web` 时网页才能搜人、加人。线上 Pages 是静态快照，只读。

## 数据从哪来

全部走币安网页端未文档化的 `bapi`，官方签名 API 查不到别人的公开组合：

| 用途 | 端点 |
| --- | --- |
| 搜人 | `POST /bapi/futures/v1/friendly/future/copy-trade/home-page/query-list`（`nickname` 模糊匹配，必须 `useAiRecommended=false`） |
| 组合概况 | `GET .../lead-portfolio/detail?portfolioId=` |
| 当前持仓 | `GET .../lead-data/positions?portfolioId=` |
| 平仓记录 | `POST .../lead-portfolio/position-history` |
| 最新订单 | `POST .../lead-portfolio/order-history` |

注意：`nickname` 搜不到纯 pid，所以粘贴链接/ID 走 `detail` 反查。这些接口没有 CORS 头，
浏览器不能直接调，必须服务端转发——这也是线上静态站无法搜索的原因。

## GitHub Actions 定时刷新

`.github/workflows/refresh.yml`，cron `7,37 * * * *` UTC：

1. `probe` 先验出口能不能访问币安（2026-11-09 实测 Azure westus3 返回 200，未被 451 拦）
2. 读 `watchlist.json` 采集 → 回写 state / 快照 / 名单
3. `build_static.py` 生成 `docs/` → Pages 发布

手动加人：Actions → 币安跟单榜刷新 → Run workflow → 填 `add_target`（ID 或链接）/ `remove_pid`。

首次部署要手动开一次 Pages（Settings → Pages → Source = GitHub Actions），
`GITHUB_TOKEN` 无权自动创建 Pages site。

## 需要配的 Secrets

| 名称 | 用途 |
| --- | --- |
| `NTFY_URL` | 自建 ntfy 地址，如 `http://1.2.3.4:8858` |
| `NTFY_TOKEN` | 只授权 `bn-*` 前缀的发布 token（服务端 `auth-default-access: deny-all`，匿名发布返回 403） |
| `SITE_URL` | 静态站地址，拼进通知的点击链接（也可配成仓库 Variable） |
| `BINANCE_PROXY` | 可选，出口被拦时才需要 |

不配 `NTFY_TOKEN` 也能跑，只是不推送，只更新网页。

## 目录

| 文件 | 作用 |
| --- | --- |
| `trader_alert.py` | 采集 + 聚合 + ntfy 推送 + 本地网页服务（`/api/data`、`/api/search`、`/api/add`、`/api/trader`） |
| `watchlist.json` | 监控名单，唯一数据源，随仓库持久化 |
| `monitor.html` | 响应式单页，PC 双栏 / 手机单列，数据来自内联快照或 `/api/data` |
| `build_static.py` | 把快照与详情内联成纯静态站，不改 `monitor.html` 源文件 |
| `docs/` | 生成的静态站（Pages 根目录） |

## 已知限制

- 指标有延迟：ROI、PNL 约每小时更新，订单历史约 4 分钟，平仓数分钟到 1 小时，不能当成交信号
- 隐藏持仓的带单员抓不到仓位（`positionShow=false`）
- Pages 是静态站，线上不能加人；加人在本机或 Actions
