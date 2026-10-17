"""把番组计划官方 weekly dump 建成**本地离线索引**。

════════ 这是"可靠的 bgm 访问"的答案 ════════
`api.bgm.tv` 挂在 Cloudflare 后面，国内可达性**是飘的**（同一台机器上，一会儿
直连 200，一会儿 000）。让用户自己配代理的开源工具没人用。
所以把 bgm 的知识**搬进本地**：

    数据源  https://github.com/bangumi/Archive  （官方每周三 05:00 GMT+8 导出）
    入口    aux/latest.json → browser_download_url + sha256
    国内下  ghfast.top/<github 原始地址>   （实测 1.3MB/s，直连也有 400KB/s）

dump 里有（README 逐字段核对过）：
    subject    id / type / name / name_cn / infobox / platform / summary /
               tags / score / rank / date / nsfw / series
    episode    id / name / name_cn / description / airdate / disc / duration /
               **subject_id** / **sort** / type
    person / character / subject-persons / subject-characters / 各类关联

我们**只需要前两个**：身份（subject）和集号映射 + 中文集名（episode）。
`infobox` 和 `description` 占了大头（986MB 里大部分是 infobox）而且用不上，丢掉。

产物：`bgm.db`（SQLite），大约几百 MB。之后：
    · 认身份      → 本地查，零网络
    · 中文名/简介  → 本地查
    · **每集中文标题** → 本地查（AnimeMan 这类工具没有的东西）
    · 海报        → 只有这个需要联网（lain.bgm.tv）

用法:
    python3 bgm_index.py build [--dump data/dump.zip] [--out data/bgm.db]
    python3 bgm_index.py check        # 只看现状
"""
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

from ..config import get as get_cfg
from ..textutil import norm_query as norm

# 路径在 build()/check() 里从配置取；模块级先用占位，避免 import 时就绑死环境。
DUMP = None
OUT = None

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = OFF;
DROP TABLE IF EXISTS subject;
DROP TABLE IF EXISTS episode;
DROP TABLE IF EXISTS sname;
DROP TABLE IF EXISTS episode_name;
CREATE TABLE subject (
  id       INTEGER PRIMARY KEY,
  type     INTEGER,
  name     TEXT,
  name_cn  TEXT,
  summary  TEXT,
  date     TEXT,
  platform INTEGER,
  score    REAL,
  rank     INTEGER,
  nsfw     INTEGER DEFAULT 0,
  tags     TEXT
);
CREATE TABLE episode (
  id         INTEGER PRIMARY KEY,
  subject_id INTEGER,
  sort       REAL,
  name       TEXT,
  name_cn    TEXT,
  airdate    TEXT,
  duration   TEXT,
  type       INTEGER,
  disc       INTEGER
);
-- 名字索引：条目名 + 中文名进同一张，norm 后等值查
CREATE TABLE sname (
  subject_id INTEGER,
  txt        TEXT,
  norm       TEXT,
  src        TEXT
);
-- 集名索引：用于"这个文件名对得上哪一集"的召回
CREATE TABLE episode_name (
  subject_id INTEGER,
  ep_sort    REAL,
  txt        TEXT,
  norm       TEXT
);
"""

def lines(zf, name):
    """从 zip 里流式读一个 jsonlines 成员 —— 不落地解压，省磁盘。"""
    with zf.open(name) as raw:
        for ln in io.TextIOWrapper(raw, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                yield json.loads(ln)


def clean_summary(s):
    """dump 的简介带全角空白和 \\r，规整一下。**不做截断**（截断会丢信息）。"""
    if not s:
        return None
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t\u3000]+", " ", s).strip() or None


def build(dump=None, out=None):
    global DUMP, OUT
    cfg = get_cfg()
    DUMP = str(dump or cfg.dump)
    OUT = str(out or cfg.bgm_db)
    if not os.path.exists(DUMP):
        print("找不到 dump: %s" % DUMP)
        return 1
    if os.path.exists(OUT):
        os.remove(OUT)
    for suf in ("-wal", "-shm"):
        p = OUT + suf
        if os.path.exists(p):
            os.remove(p)

    size = os.path.getsize(DUMP)
    print("dump: %s (%.1f MB)" % (DUMP, size / 1048576))
    con = sqlite3.connect(OUT)
    con.executescript(SCHEMA)
    t0 = time.time()

    zf = zipfile.ZipFile(DUMP)
    names = set(zf.namelist())

    # ── subject ──
    n_sub = n_nm = 0
    rows, nms = [], []
    for d in lines(zf, "subject.jsonlines"):
        sid = d.get("id")
        if sid is None:
            continue
        tags = d.get("tags") or []
        tagtxt = ",".join(t.get("name", "") for t in tags[:20] if t.get("name"))
        rows.append((sid, d.get("type"), d.get("name"), d.get("name_cn"),
                     clean_summary(d.get("summary")), d.get("date"),
                     d.get("platform"), d.get("score"), d.get("rank"),
                     1 if d.get("nsfw") else 0, tagtxt or None))
        for key in ("name", "name_cn"):
            v = (d.get(key) or "").strip()
            if v:
                nms.append((sid, v, norm(v), key))
        n_sub += 1
        n_nm += 2
        if len(rows) >= 5000:
            con.executemany("INSERT OR REPLACE INTO subject VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.executemany("INSERT INTO sname VALUES (?,?,?,?)", nms)
            rows, nms = [], []
            if n_sub % 50000 == 0:
                print("  subject %d 条  %.0fs" % (n_sub, time.time() - t0), flush=True)
    if rows:
        con.executemany("INSERT OR REPLACE INTO subject VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO sname VALUES (?,?,?,?)", nms)
    print("subject 完成 %d 条，名字 %d 条  %.0fs" % (n_sub, n_nm, time.time() - t0), flush=True)

    # ── episode ──
    n_ep = n_en = 0
    rows, nms = [], []
    for d in lines(zf, "episode.jsonlines"):
        eid = d.get("id")
        if eid is None:
            continue
        rows.append((eid, d.get("subject_id"), d.get("sort"), d.get("name"),
                     d.get("name_cn"), d.get("airdate"), d.get("duration"),
                     d.get("type"), d.get("disc")))
        for key in ("name", "name_cn"):
            v = (d.get(key) or "").strip()
            # 集名索引只收像"标题"的：太短的（"第1话"）召不回东西，但**不过滤掉**
            # —— 交给上层用，这里只做噪声底线：长度 >= 2
            if len(v) >= 2 and not v.isdigit():
                nms.append((d.get("subject_id"), d.get("sort"), v, norm(v)))
        n_ep += 1
        n_en += 1
        if len(rows) >= 20000:
            con.executemany("INSERT OR REPLACE INTO episode VALUES (?,?,?,?,?,?,?,?,?)", rows)
            con.executemany("INSERT INTO episode_name VALUES (?,?,?,?)", nms)
            rows, nms = [], []
            if n_ep % 200000 == 0:
                print("  episode %d 条  %.0fs" % (n_ep, time.time() - t0), flush=True)
    if rows:
        con.executemany("INSERT OR REPLACE INTO episode VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO episode_name VALUES (?,?,?,?)", nms)
    print("episode 完成 %d 条  %.0fs" % (n_ep, time.time() - t0), flush=True)

    print("建索引...", flush=True)
    con.execute("CREATE INDEX idx_sname_norm ON sname(norm)")
    con.execute("CREATE INDEX idx_sname_sid ON sname(subject_id)")
    con.execute("CREATE INDEX idx_ep_sub ON episode(subject_id, sort)")
    con.execute("CREATE INDEX idx_epname_norm ON episode_name(norm)")
    con.execute("CREATE INDEX idx_subject_type ON subject(type)")
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    print("\n完成：%s  %.1f MB，用时 %.0f 秒" % (
        OUT, os.path.getsize(OUT) / 1048576, time.time() - t0))
    return 0


def check(dump=None, out=None):
    global OUT
    cfg = get_cfg()
    OUT = str(out or cfg.bgm_db)
    if not os.path.exists(OUT):
        print("还没有 %s" % OUT)
        return 1
    con = sqlite3.connect(OUT)
    for t in ("subject", "episode", "sname", "episode_name"):
        try:
            n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            print("  %-14s %d" % (t, n))
        except Exception as e:
            print("  %-14s ?  %s" % (t, e))
    print("  文件大小 %.1f MB" % (os.path.getsize(OUT) / 1048576))
    print("\n动画条目（type=2）里有中文名的：")
    print("  ", con.execute("SELECT COUNT(*) FROM subject WHERE type=2 AND name_cn IS NOT NULL").fetchone()[0])
    print("有简介的动画条目：")
    print("  ", con.execute("SELECT COUNT(*) FROM subject WHERE type=2 AND summary IS NOT NULL").fetchone()[0])
    print("\n抽查：《葬送的芙莉莲》")
    for r in con.execute("SELECT id,name,name_cn,date,score FROM subject WHERE name_cn LIKE ? LIMIT 3",
                         ("%葬送的芙莉莲%",)):
        print("   ", r)
    return 0


def main(args=None):
    """`animediator bgm-build` 的入口。

    默认行为：**先确保本地数据是最新一期，再建索引。**
    不需要用户去 GitHub 找文件、也不需要记得每周更新。

        --offline   不联网，只用本地已有的 dump
        --force     无视版本判断，强制重新下载
        --check     只体检索引，不重建
    """
    cfg = get_cfg()
    a = args
    dump = getattr(a, "dump", None) if a is not None else None
    out = getattr(a, "out", None) if a is not None else None
    want_check = bool(getattr(a, "check", False)) if a is not None else False
    offline = bool(getattr(a, "offline", False)) if a is not None else False
    force = bool(getattr(a, "force", False)) if a is not None else False

    if want_check:
        return check(dump, out)

    if dump:
        # 用户明确指定了文件，就用它，不去联网
        if not Path(dump).exists():
            print("指定的文件不存在：%s" % dump)
            return 2
        return build(dump, out)

    # ── 自动获取数据 ──
    from .fetch import ensure
    print("═══ 获取离线数据 ═══")
    try:
        path, fresh = ensure(force=force, offline=offline)
    except SystemExit:
        raise
    if not path.exists():
        return 2
    print()
    return build(str(path), out)
