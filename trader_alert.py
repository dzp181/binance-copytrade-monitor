#!/usr/bin/env python3
"""
币安跟单带单员监控台 —— 自选名单 + 一人一订阅主题 + ntfy 推送。

和上一版的区别（重要）：
  上一版每轮去抓「跟单榜前 100 人」，名单由榜单算法决定。
  这一版名单来自 watchlist.json，只监控你加进来的人：
    - 搜索昵称找到人 -> 加进名单 -> 自动生成专属主题 bn-xxxxxxxx
    - 每轮只采集名单里的人，谁进谁出与榜单排序无关
    - 每人的平仓/持仓变化聚合后推到自己的 ntfy 主题

数据口径（2026-11 实测）：
  detail 接口没有 30 日 ROI/PNL，只有 lifetime 指标。所以：
    跟单人数   currentCopyCount / maxCopyCount
    跟单者盈亏 copierPnl（跟单者视角的累计盈亏，比带单员自身 ROI 更贴近用户）
    夏普       sharpRatio
    最近交易   lastTradeTime（判断这人还在不在交易，比"最近 N 笔"更有用）
  仓位/平仓/订单仍然有数分钟延迟，这是播报不是信号。

用法：
  python3 trader_alert.py --search 星辰          # 昵称模糊搜索
  python3 trader_alert.py --add 4788776444236355328
  python3 trader_alert.py --add https://www.binance.com/zh-CN/copy-trading/lead-details/4788776444236355328
  python3 trader_alert.py --list                 # 看当前名单与主题
  python3 trader_alert.py --remove 4788776444236355328
  python3 trader_alert.py --once                 # 采一轮（建基线或推送新增）
  python3 trader_alert.py --preview              # 打印将要推送的内容，不发送
  python3 trader_alert.py --test-ntfy            # 发一条自检到 ntfy
  python3 trader_alert.py --web --watch 900      # 网页监控台 + 定时刷新

推送配置（环境变量）：
  NTFY_URL    自建 ntfy 地址，默认 http://112.74.95.74:8858
  NTFY_TOKEN  ntfy 访问令牌；该服务是 deny-all，不带 token 会 403
  SITE_URL    通知里"点击查看"跳转的网页地址

兼容保留的全局渠道（可不配）：
  WECOM_WEBHOOK / TG_BOT_TOKEN+TG_CHAT_ID / PUSH_WEBHOOK
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://www.binance.com"
LIST_PATH = "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
DETAIL_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
POSITIONS_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
HISTORY_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
ORDERS_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history"
WEB_ORDER_LIMIT = 10                 # 内联到 HTML 的订单条数，按需接口给 20 条

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "clienttype": "web",
    "lang": "zh-CN",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/zh-CN/copy-trading",
    "Accept": "application/json",
}

CST = timezone(timedelta(hours=8))
STATE_FILE = "trader_alert_state.json"
WATCHLIST_FILE = "watchlist.json"
LOG_FILE = "alerts.log"
WEB_DATA_FILE = "monitor-data.json"
WEB_PAGE_FILE = "monitor.html"

NTFY_URL = (os.environ.get("NTFY_URL") or "http://112.74.95.74:8858").rstrip("/")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN") or ""
SITE_URL = (os.environ.get("SITE_URL")
            or "https://dzp181.github.io/binance-copytrade-monitor/")
TOPIC_PREFIX = "bn-"


# ---------------------------------------------------------------- 基础工具
def now_cst():
    return datetime.now(CST)


def stamp(fmt="%m-%d %H:%M"):
    return now_cst().strftime(fmt)


def fmt_ms(ms, with_date=False):
    if not isinstance(ms, (int, float)) or ms < 1e9:
        return "—"
    sec = ms / 1000 if ms > 1e12 else ms
    t = datetime.fromtimestamp(sec, CST)
    return t.strftime("%m-%d %H:%M" if with_date else "%H:%M")


def money(v, digits=2):
    try:
        return ("+" if float(v) >= 0 else "") + format(float(v), ",.%df" % digits)
    except (TypeError, ValueError):
        return "—"


def price(v):
    try:
        f = float(v)
        return ("%.2f" % f) if abs(f) >= 1 else ("%.6g" % f)
    except (TypeError, ValueError):
        return "—"


def pct(v):
    """入参是百分数（19.6 -> +19.6%），与 detail 的 roi 口径一致。"""
    try:
        return ("+" if float(v) >= 0 else "") + "%.1f%%" % float(v)
    except (TypeError, ValueError):
        return "—"


def request(path, method="GET", body=None, params=None, retries=2):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last = exc
            if i < retries:
                time.sleep(1.5 * (i + 1))
    raise last


# ---------------------------------------------------------------- 名单
def slug_for(pid):
    """一个带单员一个主题。用 pid 后 8 位：稳定、不撞名、改昵称不换主题。

    不用昵称转拼音的原因是：中文昵称要额外依赖、会撞名、改名就换主题，
    老订阅者直接收不到消息。
    """
    return TOPIC_PREFIX + str(pid)[-8:]


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return {"updated_at": None, "items": []}
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc.get("items"), list):
            doc["items"] = []
        return doc
    except Exception as exc:
        print("! watchlist.json 读取失败：%s" % exc, file=sys.stderr)
        return {"updated_at": None, "items": []}


def save_watchlist(doc):
    doc["updated_at"] = stamp("%Y-%m-%d %H:%M:%S")
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, WATCHLIST_FILE)


def watch_entries(enabled_only=True):
    """名单里的人，按加入顺序。slug/topic 缺失时按 pid 补出来。"""
    out = []
    for it in load_watchlist()["items"]:
        pid = str(it.get("pid") or "")
        if not pid.isdigit():
            continue
        if enabled_only and not it.get("enabled", True):
            continue
        it = dict(it)
        it["pid"] = pid
        it["slug"] = it.get("slug") or slug_for(pid)
        it["topic"] = it.get("topic") or it["slug"]
        out.append(it)
    return out


def find_entry(pid):
    pid = str(pid)
    for it in load_watchlist()["items"]:
        if str(it.get("pid")) == pid:
            return it
    return None


def add_watch(target, source="manual", note=""):
    """按 pid 或主页链接加入名单。已在名单里则返回原条目（幂等）。"""
    info = resolve_target(target)
    pid = info["pid"]
    doc = load_watchlist()
    for it in doc["items"]:
        if str(it.get("pid")) == pid:
            if not it.get("enabled", True):
                it["enabled"] = True
                save_watchlist(doc)
                return dict(it, _reused=True, _reenabled=True)
            return dict(it, _reused=True)
    entry = {
        "pid": pid,
        "slug": slug_for(pid),
        "topic": slug_for(pid),
        "name": info.get("name") or pid,
        "show": bool(info.get("show")),
        "enabled": True,
        "source": source,
        "note": note,
        "added_at": stamp("%Y-%m-%d %H:%M"),
    }
    doc["items"].append(entry)
    save_watchlist(doc)
    return entry


def remove_watch(pid):
    pid = str(pid)
    doc = load_watchlist()
    before = len(doc["items"])
    doc["items"] = [it for it in doc["items"] if str(it.get("pid")) != pid]
    if len(doc["items"]) == before:
        return False
    save_watchlist(doc)
    return True


def touch_watch_meta(meta):
    """用最新 detail 刷新名单里的昵称等元信息，只有真变了才写盘。"""
    doc = load_watchlist()
    changed = False
    for it in doc["items"]:
        pid = str(it.get("pid"))
        m = meta.get(pid)
        if not m:
            continue
        if m.get("name") and m["name"] != it.get("name"):
            it["name"] = m["name"]
            changed = True
        if m.get("last_trade_at") and m["last_trade_at"] != it.get("last_trade_at"):
            it["last_trade_at"] = m["last_trade_at"]
            changed = True
        if "show" in m and bool(m["show"]) != bool(it.get("show")):
            it["show"] = bool(m["show"])
            changed = True
    if changed:
        save_watchlist(doc)
    return changed


# ---------------------------------------------------------------- 搜索与解析
PID_IN_URL = re.compile(r"(\d{15,20})")


def resolve_target(text):
    """从主页链接 / 裸 pid 反查带单员。搜索接口不支持按 pid 搜，只能走 detail。"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("没有提供 pid 或链接")
    m = PID_IN_URL.search(text)
    if not m:
        raise ValueError("链接里找不到带单员 ID")
    pid = m.group(1)
    data = fetch_detail(pid)
    if not data.get("leadPortfolioId"):
        raise ValueError("查不到这个带单员（ID %s）" % pid)
    return {
        "pid": str(data.get("leadPortfolioId")),
        "name": data.get("nickname") or pid,
        "avatar": data.get("avatarUrl") or "",
        "cc": data.get("currentCopyCount") or 0,
        "max_cc": data.get("maxCopyCount") or 0,
        "copier_pnl": data.get("copierPnl"),
        "show": data.get("positionShow") is True,
        "status": data.get("status") or "",
        "paused": data.get("isPaused") is True,
    }


def _slim_leader(r):
    return {
        "pid": str(r.get("leadPortfolioId") or ""),
        "name": r.get("nickname") or "",
        "avatar": r.get("avatarUrl") or "",
        "cc": r.get("currentCopyCount") or 0,
        "max_cc": r.get("maxCopyCount") or 0,
        "roi": r.get("roi"),
        "pnl": r.get("pnl"),
        "copier_pnl": r.get("copierPnl"),
        "mdd": r.get("mdd"),
        "show": (r.get("portfolioType") or "").upper() == "PUBLIC",
        "badge": r.get("badgeName") or "",
    }

def search_leaders(nickname, limit=20):
    """昵称模糊搜索。

    两个必须注意的点（实测）：
      1. 必须走 useAiRecommended=False（「全部」tab）。推荐 tab 只返回十几个人，
         搜同一个词会漏掉大部分人。
      2. 不支持按 pid 搜，19 位数字返回 0 条 -> 粘链接走 resolve_target。
    """
    body = {
        "pageNumber": 1, "pageSize": min(30, max(10, limit)),
        "timeRange": "30D", "dataType": "COPY_COUNT",
        "favoriteOnly": False, "hideFull": False, "nickname": nickname,
        "order": "DESC", "userAsset": 0, "portfolioType": "ALL",
        "useAiRecommended": False,
    }
    doc = request(LIST_PATH, "POST", body=body)
    rows = (doc.get("data") or {}).get("list") or []
    return [_slim_leader(r) for r in rows][:limit]


# ---------------------------------------------------------------- 搜索索引
# 静态站没有后端，币安接口也没有 CORS 头，浏览器搜不了。
# 于是把「全部」榜全量导出成一个紧凑索引，页面在用户第一次搜索时懒加载，
# 之后本地过滤——不打扰任何人，也不用为一次搜索付一次网络往返。
INDEX_FILE = os.path.join("docs", "leader-index.json")
INDEX_PAGE_SIZE = 30


def _index_page(page):
    body = {
        "pageNumber": page, "pageSize": INDEX_PAGE_SIZE,
        "timeRange": "30D", "dataType": "COPY_COUNT",
        "favoriteOnly": False, "hideFull": False, "nickname": "",
        "order": "DESC", "userAsset": 0, "portfolioType": "ALL",
        "useAiRecommended": False,
    }
    doc = request(LIST_PATH, "POST", body=body)
    data = doc.get("data") or {}
    return data.get("total") or 0, data.get("list") or []


def build_index(pages=0, workers=4):
    """拉全量「全部」榜，写 docs/leader-index.json。

    只保留 pid / 昵称 / 跟单人数。头像是八十多字符的 URL，全量存进来会
    让索引从 700KB 涨到 2MB+，移动端首屏不该为搜索付这个代价——点开某人
    时再走详情接口取头像。
    """
    total, first = _index_page(1)
    if not total or not first:
        raise RuntimeError("榜单第一页没有数据，可能被限流，稍后重试")
    page_count = pages or (total + INDEX_PAGE_SIZE - 1) // INDEX_PAGE_SIZE
    rows = {}

    def take(batch):
        for r in batch:
            pid = str(r.get("leadPortfolioId") or "")
            if pid and pid not in rows:
                rows[pid] = [pid, r.get("nickname") or "",
                             int(r.get("currentCopyCount") or 0)]

    take(first)
    print("榜单 total=%d，共 %d 页，开始并发拉取（%d 并发）…" % (total, page_count, workers))
    failed = 0
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_index_page, p): p for p in range(2, page_count + 1)}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    _, batch = fut.result()
                    take(batch)
                except Exception as exc:
                    failed += 1
                    print("  第 %d 页失败：%s" % (futs[fut], exc))
                if done % 50 == 0:
                    print("  %d/%d 页，去重后 %d 人" % (done, page_count - 1, len(rows)))

    items = sorted(rows.values(), key=lambda x: -x[2])
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    payload = {"updated_at": stamp("%Y-%m-%d %H:%M:%S"),
               "total": len(items), "pages": page_count, "rows": items}
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(INDEX_FILE)
    print("索引已生成：%d 人，%d 页失败，%.0f KB -> %s" % (
        len(items), failed, size / 1024.0, INDEX_FILE))
    return payload


# ---------------------------------------------------------------- 抓取
def fetch_history(pid, size=50):
    doc = request(HISTORY_PATH, "POST", body={
        "portfolioId": str(pid), "pageNumber": 1, "pageSize": size,
    })
    return (doc.get("data") or {}).get("list") or []


def fetch_orders(pid, size=20):
    """订单流水（含开仓单）。total>0 却返回空列表属瞬时异常，重试一次。"""
    body = {"portfolioId": str(pid), "pageNumber": 1, "pageSize": size}
    doc = request(ORDERS_PATH, "POST", body=body)
    data = doc.get("data") or {}
    rows = data.get("list") or []
    total = data.get("total") or 0
    if not rows and total:
        doc = request(ORDERS_PATH, "POST", body=body)
        data = doc.get("data") or {}
        rows = data.get("list") or []
    return rows, total


def fetch_detail(pid):
    doc = request(DETAIL_PATH, params={"portfolioId": str(pid)})
    return doc.get("data") or {}


def fetch_positions(pid):
    """只返回非零仓位，key 带 positionSide 以支持双向持仓。"""
    doc = request(POSITIONS_PATH, params={"portfolioId": str(pid)})
    arr = doc.get("data") or []
    if not isinstance(arr, list):
        return None                      # 空数组 / 异常 => 不可观测，不能当空仓
    out = {}
    for x in arr:
        try:
            amt = float(x.get("positionAmount") or 0)
        except (TypeError, ValueError):
            continue
        if amt == 0:
            continue
        side = x.get("positionSide") or ("LONG" if amt > 0 else "SHORT")
        out["%s|%s" % (x.get("symbol"), side)] = {
            "amount": amt,
            "entry": x.get("entryPrice"),
            "mark": x.get("markPrice"),
            "pnl": x.get("unrealizedProfit"),
        }
    return out


# ---------------------------------------------------------------- 清洗
def classify(a0, a1, eps=1e-8):
    if a0 == 0 and a1 != 0:
        return "开仓"
    if a1 == 0:
        return "平仓"
    if (a0 > 0) != (a1 > 0):
        return "反手"
    if abs(a1) > abs(a0) + eps:
        return "加仓"
    if abs(a1) < abs(a0) - eps:
        return "减仓"
    return None


def _clean_trades(hs):
    """只保留完全平仓、带真实平仓时间的记录，按时间倒序。"""
    out = []
    for h in hs:
        closed = h.get("closed")
        if not isinstance(closed, (int, float)) or closed < 1e12:
            continue
        if h.get("status") not in (None, "All Closed"):
            continue
        opened = h.get("opened")
        hold = (round((closed - opened) / 3600000, 1)
                if isinstance(opened, (int, float)) and opened > 1e12 and closed >= opened
                else None)
        out.append({
            "id": str(h.get("positionId") or h.get("id") or ""),
            "symbol": h.get("symbol") or "—",
            "side": str(h.get("side") or ""),
            "lev": h.get("leverage"),
            "pnl": round(float(h.get("closingPnl") or 0), 2),
            "closed": closed, "opened": opened,
            "o": h.get("avgCost"), "c": h.get("avgClosePrice"),
            "hold": hold,
        })
    out.sort(key=lambda x: x["closed"], reverse=True)
    return out


def _clean_positions(pos):
    if not isinstance(pos, dict):
        return []
    out = []
    for key, v in pos.items():
        sym, _, side = key.partition("|")
        out.append({
            "symbol": sym, "side": side,
            "amount": v.get("amount"), "entry": v.get("entry"),
            "mark": v.get("mark"), "pnl": v.get("pnl"),
        })

    def _k(x):
        try:
            return abs(float(x.get("pnl") or 0))
        except (TypeError, ValueError):
            return 0.0
    out.sort(key=_k, reverse=True)
    return out


def _clean_orders(rows):
    """订单流水：保留带真实时间戳的记录，按时间倒序。

    只呈现原始买卖方向与持仓方向，**不推断开仓/平仓** —— 部分减仓、
    双向持仓、同秒多笔委托都会让推断失真。
    """
    out = []
    for o in rows or []:
        t = o.get("orderTime") or o.get("orderUpdateTime")
        if not isinstance(t, (int, float)) or t < 1e12:
            continue
        try:
            pnl = round(float(o.get("totalPnl") or 0), 2)
            px = round(float(o.get("avgPrice") or 0), 6)
            qty = round(float(o.get("executedQty") or 0), 6)
        except (TypeError, ValueError):
            continue
        out.append({
            "t": t,
            "symbol": o.get("symbol") or "—",
            "side": str(o.get("side") or "").upper(),
            "ps": str(o.get("positionSide") or "").upper(),
            "type": str(o.get("type") or "").upper(),
            "px": px, "qty": qty, "pnl": pnl,
        })
    out.sort(key=lambda x: x["t"], reverse=True)
    return out


# ---------------------------------------------------------------- 状态
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("version") != 2:
            # 旧版 seen_positions 是 100 人混合的，无法归属到人，直接重建基线
            print("· 旧版状态已废弃，本轮重建基线（不会推送历史记录）")
            return None
        doc.setdefault("seen", {})
        doc.setdefault("positions", {})
        return doc
    except Exception:
        return None


def save_state(state):
    state["version"] = 2
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- ntfy 推送
def publish_ntfy(topic, title, message, click=None, tags=None, priority=None):
    """推一条到指定主题。返回人话结果，失败不抛异常（不能因推送挂掉整轮）。"""
    if not NTFY_TOKEN:
        return "跳过（未配置 NTFY_TOKEN）"
    body = {"topic": topic, "title": title, "message": message}
    if click:
        body["click"] = click
    if tags:
        body["tags"] = tags
    if priority:
        body["priority"] = priority
    req = urllib.request.Request(
        NTFY_URL + "/",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + NTFY_TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return "ntfy %s" % resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:120]
        except Exception:
            pass
        return "ntfy %s %s" % (exc.code, detail)
    except Exception as exc:
        return "ntfy 失败：%s" % exc


def push_legacy(text):
    """保留的全局渠道，用于把整轮汇总发到企微/Telegram。"""
    wecom = os.environ.get("WECOM_WEBHOOK")
    tg_token = os.environ.get("TG_BOT_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")
    generic = os.environ.get("PUSH_WEBHOOK")
    sent = []
    if wecom:
        try:
            body = json.dumps({"msgtype": "markdown", "markdown": {"content": text}}).encode()
            req = urllib.request.Request(wecom, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                sent.append("企业微信 %s" % resp.status)
        except Exception as exc:
            sent.append("企业微信失败: %s" % exc)
    if tg_token and tg_chat:
        try:
            url = "https://api.telegram.org/bot%s/sendMessage" % tg_token
            body = urlencode({"chat_id": tg_chat, "text": text}).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15) as resp:
                sent.append("Telegram %s" % resp.status)
        except Exception as exc:
            sent.append("Telegram 失败: %s" % exc)
    if generic:
        try:
            body = json.dumps({"text": text}).encode()
            req = urllib.request.Request(generic, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                sent.append("自定义 %s" % resp.status)
        except Exception as exc:
            sent.append("自定义失败: %s" % exc)
    return sent


# ---------------------------------------------------------------- 提交队列
# 网页是静态站（GitHub Pages，https），既连不上币安也没有后端。
# 所以「用户想监控谁」不写进网页，而是写进服务器上 ntfy 的一个可匿名写入的
# 主题；Actions 每轮带 token 拉一次，校验通过才并入名单。
# 服务器因此只需要跑 ntfy：收提交、发推送，不碰币安。
QUEUE_TOPIC = os.environ.get("QUEUE_TOPIC") or "bnsubmit"
QUEUE_SEEN_FILE = "queue_seen.json"
QUEUE_MAX_PER_ROUND = int(os.environ.get("QUEUE_MAX_PER_ROUND") or 10)
QUEUE_SEEN_KEEP = 500


def load_queue_seen():
    try:
        with open(QUEUE_SEEN_FILE, encoding="utf-8") as f:
            doc = json.load(f)
        return {"ids": [str(x) for x in (doc.get("ids") or [])][-QUEUE_SEEN_KEEP:]}
    except Exception:
        return {"ids": []}


def save_queue_seen(doc):
    with open(QUEUE_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": doc["ids"][-QUEUE_SEEN_KEEP:]}, f,
                  ensure_ascii=False, separators=(",", ":"))


def fetch_queue():
    """从 ntfy 的 poll 接口拉队列消息。匿名读不到，必须带 token。"""
    if not NTFY_TOKEN:
        print("  ! 未配置 NTFY_TOKEN，跳过提交队列")
        return []
    url = "%s/%s/json?poll=1&since=all" % (NTFY_URL, QUEUE_TOPIC)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + NTFY_TOKEN, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "ignore")
    except Exception as exc:
        print("  ! 读取提交队列失败：%s" % exc)
        return []
    msgs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("event") == "message" and m.get("id"):
            msgs.append(m)
    return msgs


def process_queue(limit=None):
    """把队列里的提交并入名单，返回本轮真正新增的条目。

    失败的语义要分清：格式/ID 无效（ValueError）标记为已处理，否则每轮都会
    重试同一条垃圾；网络抖动不标记，留到下一轮。
    """
    limit = QUEUE_MAX_PER_ROUND if limit is None else limit
    msgs = fetch_queue()
    if not msgs:
        return []
    seen = load_queue_seen()
    done = set(seen["ids"])
    fresh = [m for m in msgs if m["id"] not in done]
    if not fresh:
        return []
    print("提交队列：%d 条新消息" % len(fresh))
    added = []
    for m in sorted(fresh, key=lambda x: x.get("time") or 0):
        mid = m["id"]
        raw = str(m.get("message") or "").strip()[:200]
        note = str(m.get("title") or "").strip()[:40]
        if not raw:
            seen["ids"].append(mid)
            continue
        if len(added) >= limit:
            print("  达到单轮上限 %d，其余留到下一轮" % limit)
            break
        try:
            entry = add_watch(raw, source="submit", note=note)
        except ValueError as exc:
            print("  跳过 %s：%s" % (raw[:40], exc))
            seen["ids"].append(mid)
            continue
        except Exception as exc:
            print("  %s 本轮失败，下轮重试：%s" % (raw[:40], exc))
            continue
        seen["ids"].append(mid)
        if entry.get("_reused"):
            print("  已在名单：%s" % entry.get("name"))
        else:
            added.append(entry)
            print("  收下：%s  %s" % (entry.get("name"), entry.get("topic")))
    save_queue_seen(seen)
    return added


# ---------------------------------------------------------------- 事件聚合
def build_events(items, state, min_pnl=0.0, preview=False):
    """把一轮采集结果按「人」聚合成通知。

    为什么必须聚合：一个人一轮可能同时平掉 5 笔，逐笔推就是 5 条通知，
    订阅者当天就会关掉推送。一个人一条，正文列前几条。

    返回 (events, seen_all)：
      events   [{pid,name,topic,title,message,click,priority,tags,stats}]
      seen_all {pid: set(positionId)} 本轮看到的全部平仓 id，用于更新基线
    """
    prev_seen = state.get("seen", {}) if state else {}
    prev_pos = state.get("positions", {}) if state else {}
    events, seen_all = [], {}

    for it in items:
        pid = it["pid"]
        name = it.get("name") or pid
        old_ids = prev_seen.get(pid)
        baseline_only = old_ids is None and not preview

        trades = it.get("recent") or []
        seen_all[pid] = {t["id"] for t in trades if t.get("id")}

        if baseline_only:
            continue                       # 新人首轮只建基线，不推历史

        old = set(old_ids or [])
        fresh, total = [], 0.0
        for t in trades:
            if not t.get("id") or t["id"] in old:
                continue
            if abs(float(t.get("pnl") or 0)) < min_pnl:
                continue
            fresh.append(t)
            total += float(t.get("pnl") or 0)

        pos_lines = []
        curr = it.get("pos") if it.get("show") else None
        if curr is not None and not baseline_only:
            old_map = prev_pos.get(pid) or {}
            if old_map:
                for key in set(list(old_map.keys()) + list(curr.keys())):
                    a0 = (old_map.get(key) or {}).get("amount", 0)
                    a1 = (curr.get(key) or {}).get("amount", 0)
                    kind = classify(a0, a1)
                    if kind:
                        pos_lines.append("%s %s：%s → %s" % (
                            key.split("|")[0], kind, a0, a1))

        if not fresh and not pos_lines:
            continue

        lines, head = [], []
        if fresh:
            head.append("%d 笔平仓 %s USDT" % (len(fresh), money(total)))
            for t in fresh[:5]:
                side_txt = "多" if str(t.get("side", "")).lower().startswith("long") else "空"
                hold = ""
                if t.get("hold") is not None:
                    hold = (" · 持仓 %.1fh" % t["hold"]) if t["hold"] >= 0.1 else " · 短仓"
                lines.append("%s %s %sx  %s USDT\n  %s → %s%s" % (
                    t["symbol"], side_txt, t.get("lev") or "?", money(t["pnl"]),
                    price(t.get("o")), price(t.get("c")), hold))
            if len(fresh) > 5:
                lines.append("…另有 %d 笔，点开看全部" % (len(fresh) - 5))
        if pos_lines:
            head.append("持仓变化 %d 项" % len(pos_lines))
            if fresh:
                lines.append("")
            lines.extend(pos_lines[:5])

        title = "%s · %s" % (name, "，".join(head))
        priority = 4 if abs(total) >= 5000 else 3
        tags = (["chart_with_upwards_trend"] if total >= 0
                else ["chart_with_downwards_trend"])
        events.append({
            "pid": pid,
            "name": name,
            "topic": it.get("topic") or slug_for(pid),
            "title": title,
            "message": "\n".join(lines) if lines else "（详见网页）",
            "click": "%s?pid=%s" % (SITE_URL, pid),
            "priority": priority,
            "tags": tags,
            "stats": {"trades": len(fresh), "pnl": round(total, 2),
                      "pos": len(pos_lines)},
        })
    return events, seen_all


# ---------------------------------------------------------------- 网页数据
def _row_of(it):
    """把一条采集结果转成网页列表行（与 build_web_data 同构）。"""
    pid = it["pid"]
    trades = it.get("recent") or []
    return {
        "pid": pid,
        "slug": it.get("slug") or slug_for(pid),
        "topic": it.get("topic") or slug_for(pid),
        "name": it.get("name") or pid,
        "avatar": it.get("avatar") or "",
        "cc": it.get("cc") or 0,
        "max_cc": it.get("max_cc") or 0,
        "copier_pnl": it.get("copier_pnl"),
        "sharpe": it.get("sharpe"),
        "aum": it.get("aum"),
        "show": bool(it.get("show")),
        "paused": bool(it.get("paused")),
        "status": it.get("status") or "",
        "last_trade_at": it.get("last_trade_at") or 0,
        "pos": _clean_positions(it.get("pos")) if it.get("show") else [],
        "recent": trades[:20],
        "orders": _clean_orders((it.get("orders") or {}).get("rows"))[:WEB_ORDER_LIMIT],
        "orders_total": (it.get("orders") or {}).get("total", 0),
        "trades": len(trades),
        "last": trades[0]["closed"] if trades else 0,
        "note": it.get("note") or "",
        "enabled": it.get("enabled", True),
    }


def build_web_data(items, visible):
    out = [_row_of(it) for it in items]
    return {
        "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
        "count": len(out),
        "visible": visible,
        "ntfy_url": NTFY_URL,
        "site_url": SITE_URL,
        "leaders": out,
    }


def upsert_web_row(it):
    """把刚加的人立刻写进网页数据，用户不用等下一轮刷新。

    顺序按 watchlist 排列，避免新人突兀地出现在末尾或开头。
    """
    with WEB_LOCK:
        try:
            with open(WEB_DATA_FILE, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            doc = {"leaders": []}
        rows = [r for r in (doc.get("leaders") or []) if str(r.get("pid")) != it["pid"]]
        rows.append(_row_of(it))
        order = {e["pid"]: i for i, e in enumerate(watch_entries(enabled_only=False))}
        rows.sort(key=lambda r: order.get(str(r.get("pid")), 10 ** 6))
        doc.update({
            "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "visible": sum(1 for r in rows if r.get("show")),
            "ntfy_url": NTFY_URL,
            "site_url": SITE_URL,
            "leaders": rows,
        })
        write_web_data(doc)
        return doc


def drop_web_row(pid):
    with WEB_LOCK:
        try:
            with open(WEB_DATA_FILE, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            return
        rows = [r for r in (doc.get("leaders") or []) if str(r.get("pid")) != str(pid)]
        doc.update({
            "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "visible": sum(1 for r in rows if r.get("show")),
            "leaders": rows,
        })
        write_web_data(doc)


def collect_into_web(entry):
    """后台补采一个刚加入的人，采完再更新网页数据。

    单人采集要 5~8 秒，不能让 /api/add 同步等，否则用户以为页面卡死。
    """
    try:
        it = _collect_one(entry, 20, 20)
        touch_watch_meta({it["pid"]: {
            "name": it.get("name"), "last_trade_at": it.get("last_trade_at"),
            "show": bool(it.get("show"))}})
        upsert_web_row(it)
        return it
    except Exception as exc:
        print("  ! 后台采集 %s 失败: %s" % (entry.get("name"), exc), file=sys.stderr)
        return None


def build_trader_detail(pid):
    """/api/trader：单人实时详情，三类数据并发抓取以缩短点击等待。"""
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = {
            "hist": pool.submit(fetch_history, pid, 20),
            "orders": pool.submit(fetch_orders, pid, 20),
            "detail": pool.submit(fetch_detail, pid),
        }
        res = {}
        for key, fut in tasks.items():
            try:
                res[key] = fut.result()
            except Exception:
                res[key] = None
    hs = res["hist"] or []
    orows, ototal = res["orders"] if isinstance(res["orders"], tuple) else ([], 0)
    show = isinstance(res["detail"], dict) and res["detail"].get("positionShow") is True
    pos = None
    if show:
        try:
            pos = fetch_positions(pid)
        except Exception:
            pos = None
    return {
        "pid": pid, "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
        "show": show and pos is not None,
        "pos": _clean_positions(pos) if pos is not None else [],
        "recent": _clean_trades(hs)[:20],
        "orders": _clean_orders(orows)[:20],
        "orders_total": ototal,
    }


DETAIL_CACHE = {}
DETAIL_LOCK = threading.Lock()
DETAIL_TTL = 60
SEARCH_CACHE = {}
SEARCH_TTL = 120
WEB_LOCK = threading.Lock()          # 并发写 monitor-data.json 的互斥


def cached_trader_detail(pid):
    now = time.time()
    with DETAIL_LOCK:
        hit = DETAIL_CACHE.get(pid)
        if hit and now - hit[0] < DETAIL_TTL:
            return hit[1]
    data = build_trader_detail(pid)
    with DETAIL_LOCK:
        DETAIL_CACHE[pid] = (now, data)
    return data


def cached_search(q):
    key = q.strip().lower()
    now = time.time()
    hit = SEARCH_CACHE.get(key)
    if hit and now - hit[0] < SEARCH_TTL:
        return hit[1]
    rows = search_leaders(q)
    SEARCH_CACHE[key] = (now, rows)
    return rows


def write_web_data(data):
    tmp = WEB_DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, WEB_DATA_FILE)


# ---------------------------------------------------------------- 采集
def _collect_one(entry, hist_size, order_size):
    """单个带单员：指标 + 平仓历史 + 订单流水 + 非零仓位。"""
    pid = entry["pid"]
    name = entry.get("name") or pid
    with ThreadPoolExecutor(max_workers=3) as pool:
        fd = pool.submit(fetch_detail, pid)
        fh = pool.submit(fetch_history, pid, hist_size)
        fo = pool.submit(fetch_orders, pid, order_size)
        try:
            detail = fd.result() or {}
        except Exception as exc:
            print("  ! %s detail 失败: %s" % (name, exc), file=sys.stderr)
            detail = {}
        try:
            hs = fh.result()
        except Exception as exc:
            print("  ! %s 平仓历史失败: %s" % (name, exc), file=sys.stderr)
            hs = []
        try:
            orows, ototal = fo.result()
        except Exception as exc:
            print("  ! %s 订单流水失败: %s" % (name, exc), file=sys.stderr)
            orows, ototal = [], 0

    show = detail.get("positionShow") is True
    pos = None
    if show:
        try:
            pos = fetch_positions(pid)
        except Exception:
            pos = None

    it = dict(entry)
    it.update({
        "name": detail.get("nickname") or entry.get("name") or pid,
        "avatar": detail.get("avatarUrl") or entry.get("avatar") or "",
        "cc": detail.get("currentCopyCount") or 0,
        "max_cc": detail.get("maxCopyCount") or 0,
        "copier_pnl": detail.get("copierPnl"),
        "sharpe": detail.get("sharpRatio"),
        "aum": detail.get("aumAmount"),
        "show": show and pos is not None,
        "paused": detail.get("isPaused") is True,
        "status": detail.get("status") or "",
        "last_trade_at": detail.get("lastTradeTime") or 0,
        "pos": pos,
        "recent": _clean_trades(hs),
        "orders": {"rows": orows, "total": ototal},
    })
    return it


def collect(hist_size=20, order_size=20, workers=4):
    """并发采集名单里的人；单人失败只降级自己，不影响整轮。"""
    entries = watch_entries()
    if not entries:
        return [], 0
    items, visible = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_collect_one, e, hist_size, order_size): e
                   for e in entries}
        done = {}
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                it = fut.result()
            except Exception as exc:
                print("  ! %s 采集失败: %s" % (entry.get("name"), exc), file=sys.stderr)
                continue
            done[it["pid"]] = it
    for e in entries:                      # 保持名单顺序，不随完成先后跳动
        it = done.get(e["pid"])
        if it:
            items.append(it)
            if it.get("show"):
                visible += 1
    return items, visible


def run_once(args):
    # 提交队列已经迁到 WorkBuddy 应用（网页上提交、管理员审核），
    # 这里默认不再读 ntfy 的 bnsubmit 主题，避免两条提交路径打架。
    # 真要用回旧通道，设 SUBMIT_QUEUE=1。
    if os.environ.get("SUBMIT_QUEUE") == "1" and not getattr(args, "no_queue", False):
        try:
            new_entries = process_queue()
            if new_entries:
                print("队列并入 %d 人，本轮一起采集。" % len(new_entries))
        except Exception as exc:
            print("  ! 处理提交队列出错（不影响采集）：%s" % exc, file=sys.stderr)
    entries = watch_entries()
    print("名单 %d 人，开始采集…" % len(entries))
    if not entries:
        print("名单是空的。先搜索并添加：python3 trader_alert.py --search 昵称")
        write_web_data(build_web_data([], 0))
        return

    items, visible = collect(args.hist_size, args.order_size, args.workers)
    trades_total = sum(len(i.get("recent") or []) for i in items)
    orders_total = sum(len((i.get("orders") or {}).get("rows") or []) for i in items)
    print("采集完成: %d 人，%d 人公开持仓，%d 条平仓，%d 条订单" % (
        len(items), visible, trades_total, orders_total))

    write_web_data(build_web_data(items, visible))

    meta = {i["pid"]: {"name": i.get("name"), "last_trade_at": i.get("last_trade_at"),
                       "show": bool(i.get("show"))}
            for i in items}
    touch_watch_meta(meta)

    state = load_state()
    events, seen_all = build_events(items, state, args.min_pnl, preview=args.preview)

    if state is None and not args.preview:
        save_state({
            "version": 2,
            "created_at": stamp("%Y-%m-%d %H:%M:%S"),
            "seen": {pid: sorted(ids)[:2000] for pid, ids in seen_all.items()},
            "positions": {i["pid"]: i["pos"] for i in items if i.get("show")},
        })
        print("已建立基线（%d 人）。下一轮起只推送新增事件。" % len(seen_all))
        print("状态文件:", STATE_FILE)
        return

    if not events:
        print("本轮无新事件。")
    for ev in events:
        print("\n[%s] %s" % (ev["topic"], ev["title"]))
        for line in ev["message"].split("\n"):
            print("   " + line)

    if args.preview:
        print("\n这是预览，未推送、未写状态。")
        return

    for ev in events:
        res = publish_ntfy(ev["topic"], ev["title"], ev["message"],
                           click=ev["click"], tags=ev["tags"], priority=ev["priority"])
        print("推送 %s -> %s" % (ev["topic"], res))

    digest = "\n".join("%s：%s" % (e["name"], e["title"].split(" · ", 1)[-1])
                       for e in events)
    if digest:
        for r in push_legacy("跟单监控 · %s\n%s" % (stamp(), digest)):
            print("汇总 -> %s" % r)

    merged = {}
    for pid, ids in seen_all.items():
        merged[pid] = sorted(set(ids) | set((state.get("seen") or {}).get(pid) or []))[:2000]
    save_state({
        "version": 2,
        "created_at": state.get("created_at"),
        "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
        "seen": merged,
        "positions": {i["pid"]: i["pos"] for i in items if i.get("show")},
    })


# ---------------------------------------------------------------- 网页服务
class WebHandler(BaseHTTPRequestHandler):
    """只放行网页、数据、按需详情与名单管理；state / 日志一律 404。"""
    root = os.getcwd()

    def log_message(self, fmt, *a):
        pass

    def _send(self, path, ctype):
        try:
            with open(os.path.join(self.root, path), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p in ("/", "/index.html", "/monitor.html"):
            self._send(WEB_PAGE_FILE, "text/html; charset=utf-8")
        elif p in ("/monitor-data.json", "/api/data"):
            self._send(WEB_DATA_FILE, "application/json; charset=utf-8")
        elif p == "/api/trader":
            pid = (q.get("pid") or [""])[0]
            if not pid.isdigit():
                self._send_json({"error": "bad pid"}, 400)
                return
            if not find_entry(pid):
                self._send_json({"error": "not in watchlist"}, 404)
                return
            try:
                self._send_json(cached_trader_detail(pid))
            except Exception as exc:
                self._send_json({"error": str(exc)[:120]}, 502)
        elif p == "/api/search":
            kw = (q.get("q") or [""])[0].strip()
            if not kw:
                self._send_json({"error": "empty query"}, 400)
                return
            try:
                rows = cached_search(kw)
            except Exception as exc:
                self._send_json({"error": str(exc)[:120]}, 502)
                return
            watched = {e["pid"] for e in watch_entries(enabled_only=False)}
            for r in rows:
                r["watched"] = r["pid"] in watched
            self._send_json({"q": kw, "count": len(rows), "list": rows})
        elif p == "/api/watchlist":
            self._send_json(load_watchlist())
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/add":
            target = str(body.get("target") or body.get("pid") or "").strip()
            if not target:
                self._send_json({"error": "缺少 pid 或链接"}, 400)
                return
            try:
                entry = add_watch(target, source="web")
            except Exception as exc:
                self._send_json({"error": str(exc)[:160]}, 400)
                return
            # 立刻返回，采集丢到后台（单人 5~8 秒，同步等会让页面像卡死）
            threading.Thread(target=collect_into_web, args=(entry,), daemon=True).start()
            self._send_json({"ok": True, "entry": entry, "pending": True,
                             "reused": bool(entry.get("_reused"))})
        elif u.path == "/api/remove":
            pid = str(body.get("pid") or "").strip()
            if not pid.isdigit():
                self._send_json({"error": "bad pid"}, 400)
                return
            ok = remove_watch(pid)
            if ok:
                drop_web_row(pid)
            self._send_json({"ok": ok})
        elif u.path == "/api/toggle":
            pid = str(body.get("pid") or "").strip()
            doc = load_watchlist()
            hit = False
            for it in doc["items"]:
                if str(it.get("pid")) == pid:
                    it["enabled"] = not it.get("enabled", True)
                    hit = True
                    break
            if hit:
                save_watchlist(doc)
            self._send_json({"ok": hit})
        else:
            self.send_error(404)


def serve_web(args):
    """立刻起网页（内嵌快照可读），后台采集并定时刷新。"""
    interval = args.watch or 900

    def loop():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WEB_DATA_FILE)
        try:
            fresh = (time.time() - os.path.getmtime(path)) < interval
        except OSError:
            fresh = False
        while True:
            if fresh:
                time.sleep(interval)
                fresh = False
            try:
                run_once(args)
            except Exception as exc:
                print("本轮失败: %s" % exc, file=sys.stderr)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    WebHandler.root = os.path.dirname(os.path.abspath(__file__))
    srv = ThreadingHTTPServer((args.host, args.port), WebHandler)
    print("网页监控台: http://%s:%d" % (args.host, args.port))
    print("名单 %d 人；每 %d 秒刷新。" % (len(watch_entries()), interval))
    print("Ctrl+C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


# ---------------------------------------------------------------- 命令行
def print_search(rows, kw):
    if not rows:
        print("没有搜到「%s」。换个关键词，或者直接粘主页链接用 --add。")
        return
    watched = {e["pid"] for e in watch_entries(enabled_only=False)}
    print("搜到 %d 个「%s」：\n" % (len(rows), kw))
    print("%-20s %-12s %8s %14s  %s" % ("昵称", "ID 末8位", "跟单", "跟单者盈亏", "状态"))
    for r in rows:
        flag = "已监控" if r["pid"] in watched else ""
        print("%-20s %-12s %8s %14s  %s" % (
            (r["name"] or "")[:18], r["pid"][-8:],
            r["cc"], money(r["copier_pnl"], 0), flag))
    print("\n加入监控：python3 trader_alert.py --add <pid>")


def cmd_list():
    entries = watch_entries(enabled_only=False)
    if not entries:
        print("名单为空。用 --search 找人或 --add 加人。")
        return
    print("名单 %d 人（≈%s）：\n" % (len(entries), NTFY_URL))
    print("%-20s %-12s %-6s %-6s %s" % ("昵称", "主题", "状态", "持仓", "加入时间"))
    for e in entries:
        print("%-20s %-12s %-6s %-6s %s" % (
            (e.get("name") or "")[:18], e.get("topic"),
            "监控中" if e.get("enabled", True) else "已暂停",
            "公开" if e.get("show") else "—",
            e.get("added_at") or ""))
    print("\n订阅：在 ntfy 客户端添加服务器 %s，再订阅上面的主题。" % NTFY_URL)


def cmd_test_ntfy(topic):
    topic = topic or (watch_entries() or [{}])[0].get("topic") or "bn-test"
    print("NTFY_URL   =", NTFY_URL)
    print("NTFY_TOKEN =", ("已配置(%d 字符)" % len(NTFY_TOKEN)) if NTFY_TOKEN else "未配置")
    print("topic      =", topic)
    res = publish_ntfy(topic, "自检 · 跟单监控",
                       "如果你看到这条，说明 Actions → ntfy → 手机 整条链路是通的。\n"
                       "主题：%s" % topic,
                       click=SITE_URL, tags=["white_check_mark"], priority=3)
    print("结果:", res)


def main():
    ap = argparse.ArgumentParser(description="币安跟单带单员监控（自选名单 + ntfy 订阅）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--preview", action="store_true", help="打印推送内容，不发送不写状态")
    ap.add_argument("--watch", type=int, default=0, help="循环间隔秒数，如 900")
    ap.add_argument("--search", metavar="昵称", help="按昵称模糊搜索带单员")
    ap.add_argument("--add", metavar="PID|URL", help="加入监控名单")
    ap.add_argument("--remove", metavar="PID", help="移出监控名单")
    ap.add_argument("--list", action="store_true", help="打印当前名单")
    ap.add_argument("--test-ntfy", action="store_true", help="发一条自检消息到 ntfy")
    ap.add_argument("--topic", help="配合 --test-ntfy 指定主题")
    ap.add_argument("--pull-queue", action="store_true",
                    help="只处理提交队列（网页提交的监控对象），不采集")
    ap.add_argument("--no-queue", action="store_true",
                    help="本轮不处理提交队列")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="重建全量带单员索引（供网页本地搜索）")
    ap.add_argument("--limit", type=int, default=20, help="搜索结果条数")
    ap.add_argument("--hist-size", type=int, default=20, help="每人拉取平仓记录条数")
    ap.add_argument("--order-size", type=int, default=20,
                    help="每人拉取订单流水条数（内联到网页的取前 10 条）")
    ap.add_argument("--workers", type=int, default=4, help="并发数")
    ap.add_argument("--min-pnl", type=float, default=0, help="平仓盈亏绝对值低于此值不推")
    ap.add_argument("--web", action="store_true", help="启动网页监控台")
    ap.add_argument("--host", default="127.0.0.1", help="网页监听地址")
    ap.add_argument("--port", type=int, default=8777, help="网页监听端口")
    a = ap.parse_args()

    if a.search is not None:
        print_search(search_leaders(a.search, a.limit), a.search)
        return
    if a.add:
        entry = add_watch(a.add)
        if entry.get("_reenabled"):
            print("已在名单里（之前暂停），已恢复：%s  %s" % (entry["name"], entry["topic"]))
        elif entry.get("_reused"):
            print("已经在名单里：%s  %s" % (entry["name"], entry["topic"]))
        else:
            print("已加入：%s\n  主题: %s\n  订阅: %s/%s" % (
                entry["name"], entry["topic"], NTFY_URL, entry["topic"]))
        return
    if a.remove:
        print("已移除。" if remove_watch(a.remove) else "名单里没有这个 pid。")
        return
    if a.list:
        cmd_list()
        return
    if a.test_ntfy:
        cmd_test_ntfy(a.topic)
        return
    if a.pull_queue:
        added = process_queue()
        print("队列处理完毕，新增 %d 人。" % len(added))
        return
    if a.rebuild_index:
        build_index()
        return

    if a.web:
        serve_web(a)
        return
    run_once(a)
    if a.watch:
        print("\n进入循环，每 %d 秒一轮。Ctrl+C 退出。" % a.watch)
        while True:
            time.sleep(a.watch)
            try:
                run_once(a)
            except Exception as exc:
                print("本轮失败: %s" % exc, file=sys.stderr)


if __name__ == "__main__":
    main()
