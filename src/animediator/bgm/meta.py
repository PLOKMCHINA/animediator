"""条目元数据（中文名 / 中文简介 / 海报）—— 离线优先，联网兜底。

════════ 为什么"离线优先"是这一层的核心 ════════
`api.bgm.tv` 挂在 Cloudflare 后面，**国内可达性是飘的** —— 同一台机器上
一会儿直连 200、一会儿 000。整套流程里有几百次元数据查询，靠联网就是
"能不能跑完看运气"。

而番组计划每周会放出**全量 dump**（几万部作品、几十万条别名、
一百多万条剧集）。把它建成一个本地 SQLite（`animediator bgm-build`），
之后所有查询都是毫秒级的本地等值比较 —— 这才是"可靠"的答案。

所以这一层有两个后端：

    offline   查本地 bgm.db          ← 默认，快、稳、不依赖网络
    live      api.bgm.tv             ← 本地真没有（新番／冷门）才用

`bgm.allow_network = false` 时**完全不碰网**，查不到就诚实返回 None，
让上层把它记成"待处理"，而不是卡在那里等超时。

════════ 身份怎么定？════════
不做模糊匹配、不打分。本地索引里查的是 `sname.norm` 的**等值**命中
（归一化只是把大小写/标点/形近字折平，不是放宽匹配）。
真需要模糊召回时，把关键词交给模型生成多个写法，逐个精确查 ——
**召回是代码的活，判断是模型的活。**
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

from ..config import get as _cfg
from ..textutil import norm

API = "https://api.bgm.tv"

_cache: dict = {}
_CON: sqlite3.Connection | None = None


def _cfg_obj():
    return _cfg()


# ══════════════════ 离线后端（bgm.db） ══════════════════

def _con() -> sqlite3.Connection | None:
    global _CON
    if _CON is None:
        p = _cfg_obj().bgm_db
        if not p.exists():
            return None
        _CON = sqlite3.connect(str(p), check_same_thread=False)
    return _CON


def _sub_dict(row) -> dict:
    """把本地 subject 行整成和联网接口同构的 dict —— 上层不用分两套。"""
    sid, type_, name, name_cn, summary, date, platform, score, rank, nsfw, tags = row
    return {"id": sid, "type": type_, "name": name, "name_cn": name_cn,
            "summary": summary, "date": date, "platform": platform,
            "score": score, "rank": rank, "nsfw": nsfw,
            "images": {"large": f"https://lain.bgm.tv/pic/cover/l/{sid}.jpg"
                       if sid else None},
            "tags": json.loads(tags) if tags else []}


def _offline_subject(sid):
    con = _con()
    if not con:
        return None
    r = con.execute("SELECT id,type,name,name_cn,summary,date,platform,score,"
                    "rank,nsfw,tags FROM subject WHERE id=?", (int(sid),)).fetchone()
    return _sub_dict(r) if r else None


def _offline_search(keyword, limit=8):
    """本地精确 norm 命中；没命中才退回前缀 LIKE（只做召回，不做判断）。"""
    con = _con()
    if not con:
        return []
    n = norm(keyword)
    ids = []
    if n:
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT subject_id FROM sname WHERE norm=? LIMIT ?",
            (n, limit))]
        if not ids and len(n) >= 2:
            ids = [r[0] for r in con.execute(
                "SELECT DISTINCT subject_id FROM sname WHERE norm LIKE ? LIMIT ?",
                ("%" + n + "%", limit))]
    out = []
    for sid in ids:
        s = _offline_subject(sid)
        if s and s.get("type") == 2:         # 只要番剧（type=2）
            out.append(s)
    return out[:limit]


# ══════════════════ 联网后端（api.bgm.tv） ══════════════════

def _allow_network() -> bool:
    return bool(_cfg_obj().bgm.get("allow_network", True))


def _opener():
    proxy = _cfg_obj().proxy
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, data=None, method=None, retries=3):
    ua = _cfg_obj().bgm.get("user_agent", "animediator")
    last = ""
    for k in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("User-Agent", ua)
            if data:
                req.add_header("Content-Type", "application/json")
            with _opener().open(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 500, 502, 503):
                time.sleep(2.0 * (k + 1))
                continue
            print("   [bgm HTTP %s] %s" % (ex.code, url[-60:]))
            return None
        except Exception as ex:
            last = type(ex).__name__
            time.sleep(1.5 * (k + 1))
    if last:
        print("   [bgm 取不到] %s" % last)
    return None


def _live_search(keyword, limit=8):
    body = json.dumps({"keyword": keyword, "filter": {"type": [2]}}).encode()
    d = _get(API + "/v0/search/subjects?limit=%d" % limit, data=body, method="POST")
    return (d or {}).get("data") or []


# ══════════════════ 对外接口 ══════════════════

def search(keyword, limit=8):
    """按关键词搜作品。返回 [{id,name,name_cn,date,platform,eps,images,summary}]"""
    key = "s:" + keyword
    if key in _cache:
        return _cache[key]
    out = _offline_search(keyword, limit)
    if not out and _allow_network():
        out = _live_search(keyword, limit)
    _cache[key] = out
    return out


def subject(sid):
    """取单个作品的完整字段。"""
    key = "g:%s" % sid
    if key in _cache:
        return _cache[key]
    d = _offline_subject(sid)
    if d is None and _allow_network():
        d = _get(API + "/v0/subjects/%s" % sid)
    _cache[key] = d
    return d


def episodes(sid):
    """该作品的剧集（中文集名）。离线有就秒回，没有才联网。

    ⚠️ 离线库里剧集中文名的覆盖率不代表成片质量 ——
    bgm 的剧集条目主要由用户补录，大部分 TV 番的「中文集名」是空的。
    所以集名不能指望这一层，真正兜底的是生成 NFO 时按原始文件名保留。
    """
    con = _con()
    if con:
        rows = con.execute(
            "SELECT sort,name,name_cn FROM episode WHERE subject_id=? "
            "AND type=0 ORDER BY sort", (int(sid),)).fetchall()
        if rows:
            return [{"sort": r[0], "name": r[1], "name_cn": r[2]} for r in rows]
    if not _allow_network():
        return []
    d = _get(API + "/v0/episodes?subject_id=%s&type=0&limit=200" % sid)
    return (d or {}).get("data") or []


def download(url, path) -> bool:
    """下载图片到本地。返回是否成功。"""
    import os
    if not url or os.path.exists(path):
        return os.path.exists(path)
    if not _allow_network():
        return False
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _cfg_obj().bgm.get("user_agent", "animediator"))
        with _opener().open(req, timeout=60) as r:
            data = r.read()
        if len(data) < 500:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception as ex:
        print("   [图下载失败] %s %s" % (type(ex).__name__, url[-50:]))
        return False


def describe() -> str:
    c = _cfg_obj()
    ok = c.bgm_db.exists()
    return "元数据：离线索引 %s%s，联网兜底 %s" % (
        c.bgm_db, "" if ok else "（不存在，先跑 bgm-build）",
        "开" if _allow_network() else "关")


# ── 命令行：`animediator meta search <词>` / `meta get <id>` ──
def main(args=None):
    import sys
    argv = sys.argv[1:] if args is None else list(getattr(args, "argv", []) or [])
    if len(argv) < 2:
        print(__doc__)
        print("用法：animediator meta search <关键词> | animediator meta get <bgm_id>")
        return 1
    cmd, arg = argv[0], argv[1]
    print(describe())
    if cmd == "search":
        for it in search(arg):
            print("%-9s %-34s | %s" % (it.get("id"), (it.get("name") or "")[:34],
                                       (it.get("name_cn") or "")[:34]))
    elif cmd == "get":
        d = subject(arg)
        if d:
            for k in ("id", "name", "name_cn", "date", "platform",
                      "summary"):
                print("%-10s %s" % (k, str(d.get(k))[:200]))
            print("%-10s %s" % ("poster", (d.get("images") or {}).get("large")))
        else:
            print("取不到（离线索引里没有，且联网也失败/被关掉）")
            return 1
    else:
        print("未知子命令：%s" % cmd)
        return 1
    return 0
