#!/usr/bin/env python3
"""
币安「跟单」榜牛人交易监控 + 推送。

监控对象：App 跟单页「全部」tab（16:20 实测确认）。
  「推薦 / 全部」由请求体 useAiRecommended 控制：
    True  -> 推薦，total=170，前三 熬鹰资本/玺九爺HBJX/Bran Brandon
    False -> 全部，total=12,975（本脚本默认，显式传参避免服务端改默认值）
  当前网页口径：全部 tab + 30D + dataType=COPY_COUNT + hideFull=False，
  只取前 100 人，按跟单人数降序。网页内嵌快照，断网可看；
  点击带单员 -> /api/trader 按需拉取最近平仓与当前仓位。

能推什么：
  1. 新平仓记录（所有人都有，带真实开/平仓时间、盈亏、杠杆）—— 主力
  2. 可见持仓的变化（仅 positionShow=true 的人，实测头部 3/12 可见）—— 补充
  3. 榜单指标变化（排名、跟单人数、30 日盈亏、AUM）—— 慢速播报

不能推：成交逐笔、下单时刻、隐藏者的当前仓位、秒级实时。
平仓历史有延迟（实测数分钟到约 1 小时不等），所以这是「播报」不是「跟单信号」。

用法：
  python3 trader_alert.py --web                 # 网页监控台（默认口径，推荐）
  python3 trader_alert.py --once                # 只抓一轮：建基线或推送新增事件
  python3 trader_alert.py --preview             # 生成一条真实数据示例消息
  python3 trader_alert.py --web --watch 900     # 网页 + 每 15 分钟刷新/推送
  可覆盖：--top N / --sort PNL / --tab recommended / --hist-size 50

推送渠道（环境变量，任一即可；都不设则打印到终端并写入 alerts.log）：
  WECOM_WEBHOOK     企业微信群机器人完整 URL
  TG_BOT_TOKEN + TG_CHAT_ID   Telegram
  PUSH_WEBHOOK       自定义 webhook，POST {"text": "..."}
"""
import argparse
import json
import os
import sys
import threading
import time
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
LOG_FILE = "alerts.log"
WEB_DATA_FILE = "monitor-data.json"
WEB_PAGE_FILE = "monitor.html"


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
    """roi 字段是小数，0.1852 -> +18.5%"""
    try:
        return ("+" if float(v) >= 0 else "") + "%.1f%%" % (float(v) * 100)
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


def fetch_leaderboard(top=12, data_type="PNL", tab="all"):
    """App「全部」tab：useAiRecommended=False；支持翻页（pageSize 上限 30）。"""
    body = {
        "pageNumber": 1, "pageSize": 30, "timeRange": "30D",
        "dataType": data_type, "favoriteOnly": False, "hideFull": False,
        "nickname": "", "order": "DESC", "userAsset": 0, "portfolioType": "ALL",
        "useAiRecommended": (tab == "recommended"),
    }
    rows, seen = [], set()
    for page in range(1, (top + 29) // 30 + 1):
        body["pageNumber"] = page
        doc = request(LIST_PATH, "POST", body=body)
        batch = (doc.get("data") or {}).get("list") or []
        for r in batch:
            pid = str(r.get("leadPortfolioId") or "")
            if pid and pid not in seen:
                seen.add(pid)
                rows.append(r)
        if len(batch) < 30 or len(rows) >= top:
            break
        time.sleep(0.2)
    return rows[:top]


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


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


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


def cap_text(text, limit=3500):
    """企业微信 markdown 消息上限 4096 字节，按行裁剪。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    out, used = [], 0
    for line in text.split("\n"):
        n = len((line + "\n").encode("utf-8"))
        if used + n > limit - 40:
            out.append("… 其余省略（消息过长已截断）")
            break
        out.append(line)
        used += n
    return "\n".join(out)


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


def build_web_data(leaders, histories, positions, visible, top,
                   tab="all", sort="COPY_COUNT", orders=None):
    """网页数据源：100 人目录 + 每人最近平仓 / 当前仓位，与推送共用原始响应。"""
    out = []
    for i, r in enumerate(leaders, 1):
        pid = str(r.get("leadPortfolioId"))
        trades = _clean_trades(histories.get(pid, []))
        show = pid in positions and positions[pid] is not None
        out.append({
            "rank": i, "pid": pid, "name": r.get("nickname") or pid,
            "cc": r.get("currentCopyCount") or 0,
            "pnl": round(float(r.get("pnl") or 0), 2),
            "roi": round(float(r.get("roi") or 0), 2),
            "show": show,
            "pos": _clean_positions(positions.get(pid)) if show else [],
            "recent": trades[:20],
            "orders": _clean_orders((orders or {}).get(pid, {}).get("rows"))[:WEB_ORDER_LIMIT],
            "orders_total": (orders or {}).get(pid, {}).get("total", 0),
            "trades": len(trades),
            "last": trades[0]["closed"] if trades else 0,
        })
    return {
        "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
        "tab": tab, "sort": sort, "count": len(out), "top": top,
        "visible": visible,
        "leaders": out,
    }


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


def write_web_data(data):
    tmp = WEB_DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, WEB_DATA_FILE)


class WebHandler(BaseHTTPRequestHandler):
    """只放行网页、数据与按需详情；state / 日志一律 404。"""
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

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/monitor.html"):
            self._send(WEB_PAGE_FILE, "text/html; charset=utf-8")
        elif p in ("/monitor-data.json", "/api/data"):
            self._send(WEB_DATA_FILE, "application/json; charset=utf-8")
        elif p == "/api/trader":
            pid = (parse_qs(u.query).get("pid") or [""])[0]
            if not pid.isdigit():
                self._send_json({"error": "bad pid"}, 400)
                return
            try:
                with open(os.path.join(self.root, WEB_DATA_FILE), encoding="utf-8") as f:
                    allowed = {x["pid"] for x in json.load(f).get("leaders", [])}
            except Exception:
                allowed = set()
            if pid not in allowed:
                self._send_json({"error": "unknown pid"}, 404)
                return
            try:
                self._send_json(cached_trader_detail(pid))
            except Exception as exc:
                self._send_json({"error": str(exc)[:120]}, 502)
        else:
            self.send_error(404)


def build_message(leaders, histories, positions, state, min_pnl, preview=False, hours=24):
    """生成中文推送文本。preview=True 时用最近 N 小时平仓直接演示。"""
    cutoff_ms = (time.time() - hours * 3600) * 1000
    seen = set(state.get("seen_positions", [])) if state else set()
    prev_lb = state.get("leaderboard", {}) if state else {}
    prev_pos = state.get("positions", {}) if state else {}

    sec_trades = []
    for i, r in enumerate(leaders, 1):
        pid = str(r.get("leadPortfolioId"))
        name = r.get("nickname") or pid
        items, dedup = [], set()
        for h in histories.get(pid, []):
            hid = str(h.get("positionId") or h.get("id") or "")
            if hid in dedup:
                continue
            dedup.add(hid)
            closed = h.get("closed")
            # closed 为空 / 状态非完全平仓 => 仍在持仓，不能当平仓播报
            if not isinstance(closed, (int, float)) or closed < 1e12:
                continue
            if h.get("status") not in (None, "All Closed"):
                continue
            is_new = preview or (hid and hid not in seen)
            if not is_new:
                continue
            if preview and closed < cutoff_ms:
                continue
            pnl = h.get("closingPnl") or 0
            if abs(float(pnl)) < min_pnl and not preview:
                continue
            hold = ""
            opened = h.get("opened")
            if isinstance(opened, (int, float)) and opened > 1e12:
                hrs = (closed - opened) / 3600000
                if hrs >= 0.1:
                    hold = " · 持仓 %.1fh" % hrs
                else:
                    hold = " · 短仓"
            side = "多" if str(h.get("side", "")).lower().startswith("long") else "空"
            # 接口 roi 与 closingPnl 口径经常不一致，推送只报绝对盈亏
            items.append("  %s %s %sx  %s USDT  %s\n    %s → %s%s" % (
                h.get("symbol"), side, h.get("leverage") or "?",
                money(pnl), fmt_ms(closed, with_date=True),
                price(h.get("avgCost")), price(h.get("avgClosePrice")), hold,
            ))
        if items:
            more = "（另有 %d 笔）" % (len(items) - 2) if len(items) > 2 else ""
            sec_trades.append("%s（榜%d · 跟单%s）%s\n%s" % (
                name, i, r.get("currentCopyCount"), more, "\n".join(items[:2])))

    sec_pos = []
    vis_summary = []
    for i, r in enumerate(leaders, 1):
        pid = str(r.get("leadPortfolioId"))
        curr = positions.get(pid)
        if curr is None:
            continue                                   # 隐藏持仓，跳过
        old = prev_pos.get(pid) or {}
        if preview:
            if curr:
                vis_summary.append("%s %d 个" % (r.get("nickname"), len(curr)))
            continue
        if not old:
            continue                                   # 首帧只建基线
        evs = []
        for key in set(list(old.keys()) + list(curr.keys())):
            a0 = (old.get(key) or {}).get("amount", 0)
            a1 = (curr.get(key) or {}).get("amount", 0)
            kind = classify(a0, a1)
            if kind:
                evs.append("%s %s：%s → %s" % (key.split("|")[0], kind, a0, a1))
        if evs:
            sec_pos.append("  %s：%s" % (r.get("nickname"), "；".join(evs[:4])))

    sec_lb = []
    if state and not preview:
        for i, r in enumerate(leaders, 1):
            pid = str(r.get("leadPortfolioId"))
            old = prev_lb.get(pid)
            if not old:
                sec_lb.append("  %s 新进 Top %d" % (r.get("nickname"), len(leaders)))
                continue
            name = r.get("nickname")
            if old.get("rank", i) - i >= 3:
                sec_lb.append("  %s 排名 %d → %d" % (name, old.get("rank"), i))
            dcc = (r.get("currentCopyCount") or 0) - (old.get("cc") or 0)
            if abs(dcc) >= 10:
                sec_lb.append("  %s 跟单人数 %+d → %s" % (name, dcc, r.get("currentCopyCount")))
            dpnl = (r.get("pnl") or 0) - (old.get("pnl") or 0)
            if abs(dpnl) >= max(1000, abs(old.get("pnl") or 0) * 0.05):
                sec_lb.append("  %s 30日盈亏 %+s → %s USDT" % (
                    name, money(dpnl, 0), money(r.get("pnl"), 0)))
            daum = (r.get("aum") or 0) - (old.get("aum") or 0)
            if old.get("aum") and abs(daum) / abs(old["aum"]) >= 0.10:
                sec_lb.append("  %s AUM 变动 %+.1f%%" % (name, 100 * daum / old["aum"]))

    header = "跟单牛人榜 · 交易播报  %s  Top %d" % (stamp(), len(leaders))
    if preview:
        header += "  示例/近%d小时" % hours
    parts = [header]
    if sec_trades:
        parts.append("\n新平仓\n" + "\n\n".join(sec_trades[:8]))
    if sec_pos:
        parts.append("\n持仓变化\n" + "\n".join(sec_pos[:5]))
    if vis_summary:
        parts.append("\n公开持仓 %d/%d 人：%s" % (
            len(vis_summary), len(leaders), "、".join(vis_summary[:5])))
    if sec_lb:
        parts.append("\n榜单变化\n" + "\n".join(sec_lb[:8]))
    parts.append("\n平仓记录有延迟；隐藏持仓者不报仓位。")
    return cap_text("\n".join(parts))


def push(text):
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
    if not sent:
        sent.append("本地（未配置推送渠道）")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("\n===== %s =====\n%s\n" % (stamp("%Y-%m-%d %H:%M:%S"), text))
    return sent


def _collect_one(r, hist_size, order_size):
    """单个带单员：平仓历史 + 订单流水 + 持仓可见性 + 非零仓位。"""
    pid = str(r.get("leadPortfolioId"))
    name = r.get("nickname") or pid
    with ThreadPoolExecutor(max_workers=2) as pool:
        fh = pool.submit(fetch_history, pid, hist_size)
        fo = pool.submit(fetch_orders, pid, order_size)
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
    show = False
    try:
        show = fetch_detail(pid).get("positionShow") is True
    except Exception:
        pass
    pos = None
    if show:
        try:
            pos = fetch_positions(pid)
        except Exception:
            pos = None
    return pid, hs, pos, show, {"rows": orows, "total": ototal}


def collect(top, hist_size, sort="COPY_COUNT", tab="all", workers=4,
            order_size=20):
    """并发采集；单人失败只降级自己，不影响整轮。"""
    leaders = fetch_leaderboard(top, sort, tab)
    histories, positions, orders = {}, {}, {}
    visible = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_collect_one, r, hist_size, order_size) for r in leaders]
        for fut in as_completed(futures):
            pid, hs, pos, show, od = fut.result()
            histories[pid] = hs
            orders[pid] = od
            if show:
                visible += 1
                positions[pid] = pos
    return leaders, histories, positions, visible, orders


def run_once(args):
    leaders, histories, positions, visible, orders = collect(
        args.top, args.hist_size, args.sort, args.tab,
        order_size=args.order_size)
    state = load_state()
    print("抓取完成: %d 个带单员，%d 个可见持仓，%d 条平仓记录，%d 条订单流水" % (
        len(leaders), visible, sum(len(v) for v in histories.values()),
        sum(len(v["rows"]) for v in orders.values())))
    write_web_data(build_web_data(leaders, histories, positions, visible, args.top,
                                  args.tab, args.sort, orders=orders))

    if args.preview:
        text = build_message(leaders, histories, positions, None,
                             args.min_pnl, preview=True, hours=args.preview_hours)
        print("\n" + "=" * 56 + "\n" + text + "\n" + "=" * 56)
        print("\n这是示例消息，未写入基线，也未推送。")
        return

    if state is None:
        # 首次运行只建基线，避免把历史记录一次性刷屏
        seen = [str(h.get("positionId") or h.get("id"))
                for hs in histories.values() for h in hs
                if (h.get("positionId") or h.get("id"))
                and isinstance(h.get("closed"), (int, float))
                and h.get("status") in (None, "All Closed")]
        save_state({
            "created_at": stamp("%Y-%m-%d %H:%M:%S"),
            "seen_positions": sorted(set(seen), reverse=True)[:5000],
            "leaderboard": {str(r["leadPortfolioId"]): {
                "rank": i, "pnl": r.get("pnl"), "roi": r.get("roi"),
                "cc": r.get("currentCopyCount"), "aum": r.get("aum"),
                "mdd": r.get("mdd"),
            } for i, r in enumerate(leaders, 1)},
            "positions": {k: v for k, v in positions.items() if v is not None},
        })
        print("已建立基线（%d 条历史平仓）。下一轮起只推送新增事件。" % len(seen))
        print("状态文件:", STATE_FILE)
        return

    text = build_message(leaders, histories, positions, state, args.min_pnl)
    has_event = any(s in text for s in ("\n新平仓\n", "\n持仓变化\n", "\n榜单变化\n"))
    if has_event:
        print("\n" + text + "\n")
        print("推送:", ", ".join(push(text)))
    else:
        print("本轮无新事件。")

    # 更新状态
    seen = set(state.get("seen_positions", []))
    for hs in histories.values():
        for h in hs:
            hid = str(h.get("positionId") or h.get("id") or "")
            if hid:
                seen.add(hid)
    save_state({
        "created_at": state.get("created_at"),
        "updated_at": stamp("%Y-%m-%d %H:%M:%S"),
        "seen_positions": sorted(seen, reverse=True)[:5000],
        "leaderboard": {str(r["leadPortfolioId"]): {
            "rank": i, "pnl": r.get("pnl"), "roi": r.get("roi"),
            "cc": r.get("currentCopyCount"), "aum": r.get("aum"),
            "mdd": r.get("mdd"),
        } for i, r in enumerate(leaders, 1)},
        "positions": {k: v for k, v in positions.items() if v is not None},
    })


def serve_web(args):
    """立刻起网页（内嵌快照可读），后台采集目录/详情并定时刷新。"""
    interval = args.watch or 900

    def loop():
        # 刚跑过 --once 时数据还新鲜，先等一个周期，避免重复抓 100 人
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
    print("每 %d 秒刷新；首轮在后台采集，期间页面显示内嵌快照。" % interval)
    print("Ctrl+C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


def main():
    ap = argparse.ArgumentParser(description="币安跟单榜牛人交易监控推送")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--preview", action="store_true", help="生成示例消息，不建基线不推送")
    ap.add_argument("--watch", type=int, default=0, help="循环间隔秒数，如 600")
    ap.add_argument("--top", type=int, default=100, help="网页显示人数（默认 100）")
    ap.add_argument("--tab", choices=["all", "recommended"], default="all",
                    help="App 的「全部」/「推薦」tab")
    ap.add_argument("--sort", default="COPY_COUNT",
                    choices=["PNL", "COPY_COUNT", "ROI", "AUM", "MDD", "WIN_RATE", "COPIER_PNL"],
                    help="榜单排序口径（默认跟单人数）")
    ap.add_argument("--hist-size", type=int, default=20, help="每人拉取平仓记录条数")
    ap.add_argument("--order-size", type=int, default=20,
                    help="每人拉取订单流水条数（内联到网页的取前 10 条）")
    ap.add_argument("--min-pnl", type=float, default=0, help="平仓盈亏绝对值低于此值不推")
    ap.add_argument("--preview-hours", type=int, default=24, help="示例消息回看小时数")
    ap.add_argument("--web", action="store_true", help="启动网页监控台")
    ap.add_argument("--host", default="127.0.0.1", help="网页监听地址")
    ap.add_argument("--port", type=int, default=8777, help="网页监听端口")
    a = ap.parse_args()

    if a.web:
        serve_web(a)
        return
    if a.preview:
        run_once(a)
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
