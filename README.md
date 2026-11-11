# 币安跟单监控台

自选带单员的仓位 / 平仓 / 成交监控，一人一个 ntfy 主题，网页可搜索添加、管理员审核。

**网页**：https://binance-copytrade.app.workbuddy.host/?sharecode=MW4ZSNU26Z

## 三层分工（重要）

| 谁 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| **WorkBuddy 应用** | 用户、名单、审核、搜索索引、页面；数据存同目录 SQLite | 不能出网（沙箱 `Network is unreachable`） |
| **GitHub Actions**（本仓库） | 出网采集币安、推 ntfy、把快照回写应用 | 不持有名单，不改名单 |
| **你的服务器** `112.74.95.74:8858` | 只跑 ntfy，收消息推手机 | 连不上币安（TCP 层不通，实测） |

所以**名单的唯一来源是网页**。Actions 每轮先 `pull` 再采集，在仓库里手改
`watchlist.json` 会被下一轮覆盖。

## 一轮的流程

```
Actions: pull 名单 -> trader_alert.py --once -> POST 快照回应用 -> 提交 state
网页:    读应用里的快照 -> 列表 + 详情三 tab（仓位 / 平仓 / 交易）
```

- 每 30 分钟：`.github/workflows/refresh.yml`（cron `7,37 * * * *`）
- 每天 03:17 UTC：重建全量带单员索引（约 1.3 万人），供网页昵称搜索
- 每天 04:07 UTC：把应用的 `data.db` 拉回仓库 `backups/`，留 14 天

## 目录

| 文件 | 用途 |
| --- | --- |
| `bnapp/` | WorkBuddy 应用：`server.py`（单端口 API）+ `db.py`（SQLite）+ `index.html`（单页前端） |
| `sync_app.py` | Actions 与应用之间的桥：`pull` / `push` / `index` / `backup` |
| `trader_alert.py` | 采集、差分、ntfy 推送、本地网页服务 |
| `watchlist.json` | 名单缓存，每轮由应用覆盖，**不要手改** |
| `trader_alert_state.json` | 差分基线（见过哪些仓位/平仓），跨轮持久化 |
| `monitor-data.json` | 最近一轮采集结果 |

## 需要的配置

仓库 Settings → Secrets and variables → Actions：

| 名字 | 类型 | 值 |
| --- | --- | --- |
| `APP_TOKEN` | secret | 应用「管理 → 设置」里那串 `bnat_…` |
| `APP_URL` | variable | `https://binance-copytrade.app.workbuddy.host` |
| `APP_SHARE` | variable | `MW4ZSNU26Z`（应用的每个请求都要带，否则网关回「链接不完整」） |
| `NTFY_URL` | secret | `http://112.74.95.74:8858` |
| `NTFY_TOKEN` | secret | ntfy 的 `bnbot` 令牌，只授权 `bn-*` 主题 |
| `SITE_URL` | variable | 网页地址，用于推送里的跳转链接 |
| `BINANCE_PROXY` | secret | 可选。万一 runner 出口被 451，填代理即可恢复 |

## 权限与限制

- 第一个注册的账号自动是管理员；之后注册的都是普通用户，由管理员在「管理 → 用户」里提权。
- 每人每天最多提交 3 个、待审最多 5 个，防止刷单。
- 推送按人按轮聚合：一轮里同一人多笔平仓合成一条，不刷屏。
- 采集延迟：订单历史约 4 分钟，平仓数分钟到 1 小时，ROI/PNL 每小时更新。**这不是成交信号**。

## 备份与恢复

`backups/data-YYYYMMDD.db` 是完整 SQLite 快照，含用户、名单、快照、索引。
恢复：把文件改名 `data.db` 放回应用目录重新部署（沙箱只覆盖代码文件，不删数据文件）。

## 本机调试

```bash
python3 trader_alert.py --web --watch 900     # http://127.0.0.1:8777
python3 trader_alert.py --search 星辰
python3 trader_alert.py --add 4788776444236355328
python3 trader_alert.py --test-ntfy

cd bnapp && DB_PATH=/tmp/t.db INSECURE_COOKIE=1 PORT=3999 python3 server.py
```

## 已废弃

`docs/` 与 GitHub Pages 是上一版（固定前 100 人榜单）的静态站，已停止更新，
新页面在 WorkBuddy 应用里。
