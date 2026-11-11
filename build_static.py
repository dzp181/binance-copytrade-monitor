#!/usr/bin/env python3
"""把本地监控台打包成完全自包含的静态站（供 GitHub Pages 使用）。

不修改 monitor.html 源文件：所有路径改写只发生在生成出来的 docs/index.html。
  var DATA = {...}      -> 替换为最新快照
  /api/data             -> ./monitor-data.json
  /api/trader?pid=xxx   -> ./trader/<pid>.json
  var ADMIN = true      -> false（线上没有后端，隐藏写操作、搜索改为说明）

用法：python3 build_static.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs")
DATA_FILE = "monitor-data.json"
PAGE_FILE = "monitor.html"


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def main():
    with open(os.path.join(ROOT, DATA_FILE), encoding="utf-8") as f:
        data = json.load(f)
    with open(os.path.join(ROOT, PAGE_FILE), encoding="utf-8") as f:
        html = f.read()

    # 1) 用 JSON 解析器定位内联快照，避免正则被大括号误伤
    marker = "var DATA = "
    i = html.find(marker)
    if i < 0:
        raise SystemExit("monitor.html 里找不到 var DATA = ...")
    start = i + len(marker)
    _, end = json.JSONDecoder().raw_decode(html[start:])
    if html[start + end:start + end + 1] != ";":
        raise SystemExit("内联快照格式异常")
    html = html[:start] + compact(data) + html[start + end:]

    # 2) 静态站没有后端，改读同目录 JSON
    html, n1 = re.subn(r"fetch\('/api/data'\)", "fetch('./monitor-data.json')", html)
    html, n2 = re.subn(
        r"fetch\('/api/trader\?pid='\s*\+\s*encodeURIComponent\(pid\)\)",
        "fetch('./trader/' + encodeURIComponent(pid) + '.json')",
        html,
    )
    html, n3 = re.subn(r"var ADMIN = true;", "var ADMIN = false;", html)
    if n1 != 1 or n2 != 1 or n3 != 1:
        raise SystemExit("路径改写失败：api/data=%d, api/trader=%d, ADMIN=%d" % (n1, n2, n3))

    # 3) 每个带单员一份详情 JSON，结构与 /api/trader 返回值一致
    trader_dir = os.path.join(OUT, "trader")
    os.makedirs(trader_dir, exist_ok=True)
    keep = set()
    for r in data.get("leaders", []):
        pid = str(r["pid"])
        if not pid.isdigit():
            continue
        keep.add(pid + ".json")
        detail = {
            "pid": pid,
            "updated_at": data.get("updated_at"),
            "show": bool(r.get("show")),
            "pos": r.get("pos") or [],
            "recent": r.get("recent") or [],
            "orders": r.get("orders") or [],
            "orders_total": r.get("orders_total") or 0,
        }
        with open(os.path.join(trader_dir, pid + ".json"), "w", encoding="utf-8") as f:
            f.write(compact(detail))

    # 4) 名单变小后，清掉上一版留下的人（否则会一直挂在线上）
    removed = 0
    for name in os.listdir(trader_dir):
        if name.endswith(".json") and name not in keep:
            os.remove(os.path.join(trader_dir, name))
            removed += 1

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUT, DATA_FILE), "w", encoding="utf-8") as f:
        f.write(compact(data))

    print("已生成 docs/index.html，%d 个带单员，快照时间 %s，清理旧详情 %d 份"
          % (len(data.get("leaders", [])), data.get("updated_at"), removed))


if __name__ == "__main__":
    main()
