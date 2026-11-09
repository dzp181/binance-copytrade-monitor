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

