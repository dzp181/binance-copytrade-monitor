#!/usr/bin/env python3
"""GitHub Actions <-> WorkBuddy 应用 的同步桥。

分工：
  应用（沙箱）  用户、名单、审核、搜索索引、页面 —— 但不能出网
  Actions      能出网，负责采集币安、推 ntfy、回写快照

所以每次运行都是「先拉名单，再采集，再回写」，应用始终是名单的唯一来源。

用法：
  python3 sync_app.py pull      # 拉已批准名单 -> watchlist.json
  python3 sync_app.py push      # monitor-data.json -> 应用快照
  python3 sync_app.py index     # 重建全量索引并推到应用（给网页搜索用）
  python3 sync_app.py backup    # 下载 data.db 到 backups/

环境变量：
  APP_URL    应用地址，如 https://xxx.app.workbuddy.host
  APP_TOKEN  Actions 令牌（应用「管理 -> 设置」里可复制）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.json")
MONITOR = os.path.join(HERE, "monitor-data.json")
BACKUPS = os.path.join(HERE, "backups")

APP_URL = (os.environ.get("APP_URL") or "").rstrip("/")
APP_TOKEN = os.environ.get("APP_TOKEN") or ""
# 应用的每个请求都要带分享码，否则网关直接返回「链接不完整」的 HTML。
APP_SHARE = os.environ.get("APP_SHARE") or ""


def call(path, body=None, timeout=120, raw=False):
    if not APP_URL:
        raise SystemExit("APP_URL 没配")
    url = APP_URL + path
    if APP_SHARE:
        url += ("&" if "?" in url else "?") + "sharecode=" + APP_SHARE
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-Actions-Token", APP_TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                blob = r.read()
                return blob if raw else json.loads(blob or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "ignore")
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise SystemExit("%s -> HTTP %d %s" % (path, exc.code, detail))
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise SystemExit("%s -> %s" % (path, exc))


def pull():
    doc = call("/api/a/watchlist")
    items = doc.get("items") or []
    out = []
    for it in items:
        pid = str(it.get("pid") or "")
        if not pid.isdigit():
            continue
        out.append({
            "pid": pid,
            "slug": it.get("slug") or ("bn-" + pid[-8:]),
            "topic": it.get("slug") or ("bn-" + pid[-8:]),
            "name": it.get("name") or pid,
            "enabled": True,
            "source": "app",
            "note": "",
            "added_at": time.strftime("%Y-%m-%d %H:%M"),
        })
    payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": out}
    with open(WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("已从应用拉取名单：%d 人 -> watchlist.json" % len(out))
    if not out:
        print("! 名单为空，本轮不会采集任何人（在网页上提交并批准后才会有人）")
    return len(out)


def push():
    try:
        with open(MONITOR, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as exc:
        raise SystemExit("读不到 monitor-data.json：%s" % exc)
    rows = doc.get("leaders") or []
    total = 0
    CHUNK = 8
    for i in range(0, len(rows), CHUNK):
        batch = [{"pid": r.get("pid"), "data": r} for r in rows[i:i + CHUNK]]
        res = call("/api/a/ingest", {"rows": batch}, timeout=90)
        total += res.get("saved", 0)
        print("  回写 %d/%d" % (min(i + CHUNK, len(rows)), len(rows)))
    # 最后再发一次元信息（ntfy 地址、站点地址）
    call("/api/a/ingest", {
        "rows": [],
        "ntfy_url": os.environ.get("NTFY_URL") or "",
        "site_url": os.environ.get("SITE_URL") or APP_URL,
    }, timeout=30)
    print("快照已回写：%d 人" % total)
    return total


def push_index():
    import trader_alert as T
    payload = T.build_index()
    rows = payload.get("rows") or []
    CHUNK = 2000
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        done = i + CHUNK >= len(rows)
        res = call("/api/a/index", {"reset": i == 0, "rows": part, "done": done},
                   timeout=120)
        print("  推索引 %d/%d，服务端 %d 条" % (min(i + CHUNK, len(rows)),
                                          len(rows), res.get("size", 0)))
    print("索引已推送：%d 人" % len(rows))
    return len(rows)


def backup():
    blob = call("/api/a/backup", raw=True, timeout=180)
    os.makedirs(BACKUPS, exist_ok=True)
    name = "data-%s.db" % time.strftime("%Y%m%d")
    path = os.path.join(BACKUPS, name)
    with open(path, "wb") as f:
        f.write(blob)
    # 顺手清掉 14 天前的备份，免得仓库无限长
    cutoff = time.time() - 14 * 86400
    for fn in os.listdir(BACKUPS):
        p = os.path.join(BACKUPS, fn)
        if fn.endswith(".db") and os.path.getmtime(p) < cutoff:
            os.remove(p)
    print("备份已下载：%s（%.0f KB）" % (path, len(blob) / 1024.0))
    return path


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    {"pull": pull, "push": push, "index": push_index, "backup": backup}[cmd]()


if __name__ == "__main__":
    main()
