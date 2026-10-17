"""建库 —— 把本地番库和跨库 ID 映射拼成一张 anime 表（SQLite）。

数据来源（全部本地，不依赖任何 API）：
  aodb.json          41537 条：标题/类型/集数/年份/封面/别名/关联作品
  bangumi-data.json   8897 条：跨站 ID（anidb/tmdb/mal/aniList/bangumi）+ 简体中文标题

桥接路线（优先级从高到低）：
  aodb ──(anidb id)──→ bangumi-data ──→ bangumi id + 中文名
  aodb ──(mal id)───→ bangumi-data
  aodb ──(aniList id)→ bangumi-data
  aodb ──(tmdb id)──→ bangumi-data
"""
import json
import re
import sqlite3

from ..config import get as get_cfg
from ..textutil import norm

# 这两份是**外部数据**（aodb = anidb 抓的标题库；bangumi-data = 跨站 ID 映射）。
# 放在 data/ 下，来源见 README。没配也能跑 —— 只是这一步的召回会差一些。
AODB = None
BANGUMI = None
DB = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
  aid        INTEGER PRIMARY KEY,
  title      TEXT,      -- aodb 主标题
  type       TEXT,
  episodes   INTEGER,
  year       INTEGER,
  season     TEXT,
  picture    TEXT,
  anidb      INTEGER,
  mal        INTEGER,
  anilist    INTEGER,
  tmdb       TEXT,
  bangumi    INTEGER,   -- 番组计划 subject id
  zh_name    TEXT,      -- 中文名（bangumi-data 的 titleTranslate.zh-Hans）
  meta_json  TEXT,      -- bgm.tv 抓来的完整元数据（后续填）
  meta_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_anime_anidb   ON anime(anidb);
CREATE INDEX IF NOT EXISTS idx_anime_mal     ON anime(mal);
CREATE INDEX IF NOT EXISTS idx_anime_bangumi ON anime(bangumi);

CREATE TABLE IF NOT EXISTS name (
  aid   INTEGER,
  txt   TEXT,      -- 原始写法
  norm  TEXT,      -- 归一化后
  src   TEXT       -- aodb_title / synonym / zh / bgm
);
CREATE INDEX IF NOT EXISTS idx_name_norm ON name(norm);
CREATE INDEX IF NOT EXISTS idx_name_aid  ON name(aid);

CREATE TABLE IF NOT EXISTS relate (
  aid   INTEGER,
  other INTEGER
);
CREATE INDEX IF NOT EXISTS idx_relate_aid ON relate(aid);

CREATE TABLE IF NOT EXISTS file (
  fid     INTEGER PRIMARY KEY,
  disk    TEXT,
  unit    TEXT,
  relpath TEXT UNIQUE,
  fname   TEXT,
  aid     INTEGER,
  ep      INTEGER,
  kind    TEXT,     -- 正片 / 特典
  what    TEXT,     -- 特典说明
  conf    TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_aid ON file(aid);
"""

_CONF = None


def _sid(url, host):
    m = re.search(r"https?://%s/(?:anime/)?([0-9]+)" % re.escape(host), url)
    return int(m.group(1)) if m else None


def build():
    global AODB, BANGUMI, DB
    cfg = get_cfg()
    AODB = str(cfg.data_dir / "aodb.json")
    BANGUMI = str(cfg.data_dir / "bangumi-data.json")
    DB = str(cfg.db)
    for p, what in ((AODB, "aodb.json"), (BANGUMI, "bangumi-data.json")):
        import os
        if not os.path.exists(p):
            print("缺少 %s：%s" % (what, p))
            print("  aodb.json      https://github.com/windrises/dialogue.moe 同类抓取，"
                  "或先用 bgm-build 的索引替代")
            print("  bangumi-data   https://github.com/bangumi-data/bangumi-data")
            return 2
    print("① 读 aodb …")
    aodb = json.load(open(AODB, encoding="utf-8"))["data"]
    print("   %d 条" % len(aodb))

    print("② 读 bangumi-data（跨站 ID + 中文名）…")
    bg = json.load(open(BANGUMI, encoding="utf-8"))["items"]
    by_anidb, by_mal, by_anilist, by_tmdb = {}, {}, {}, {}
    n_zh = 0
    for it in bg:
        sites = {}
        for s in it.get("sites", []):
            if s.get("site"):
                sites[s["site"]] = str(s.get("id"))
        zh = None
        tt = it.get("titleTranslate") or {}
        for k in ("zh-Hans", "zh-CN", "zh"):
            if tt.get(k):
                zh = tt[k][0] if isinstance(tt[k], list) else tt[k]
                break
        if zh:
            n_zh += 1
        rec = {"bangumi": sites.get("bangumi"), "zh": zh,
               "title": it.get("title"), "type": it.get("type")}
        if sites.get("anidb"):
            by_anidb[sites["anidb"]] = rec
        if sites.get("mal"):
            by_mal[sites["mal"]] = rec
        if sites.get("aniList"):
            by_anilist[sites["aniList"]] = rec
        if sites.get("tmdb"):
            by_tmdb[sites["tmdb"].split("/")[-1]] = rec
    print("   %d 条，其中带简体中文名 %d 条" % (len(bg), n_zh))
    print("   索引：anidb %d / mal %d / anilist %d / tmdb %d" % (
        len(by_anidb), len(by_mal), len(by_anilist), len(by_tmdb)))

    print("③ 建表 …")
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    hit = {"anidb": 0, "mal": 0, "anilist": 0, "tmdb": 0, "none": 0}
    n_name = 0
    rows, names, rels = [], [], []
    for i, x in enumerate(aodb):
        mal = anilist = anidb = None
        tmdb = None
        for s in x.get("sources", []):
            if not mal and "myanimelist.net" in s:
                mal = _sid(s, "myanimelist.net")
            elif not anilist and "anilist.co" in s:
                anilist = _sid(s, "anilist.co")
            elif not anidb and "anidb.net" in s:
                anidb = _sid(s, "anidb.net")
        # 依次用 anidb / mal / anilist / tmdb 找 bangumi 记录
        rec = None
        for key, table, tag in ((anidb, by_anidb, "anidb"),
                                (mal, by_mal, "mal"),
                                (anilist, by_anilist, "anilist")):
            if key is not None and str(key) in table:
                rec = table[str(key)]
                hit[tag] += 1
                break
        if rec is None:
            hit["none"] += 1
        bgm_id = rec.get("bangumi") if rec else None
        zh = rec.get("zh") if rec else None
        yr = None
        seas = x.get("animeSeason") or {}
        if seas.get("year"):
            yr = seas["year"]
        rows.append((i, x.get("title"), x.get("type"), x.get("episodes"), yr,
                     seas.get("season"), x.get("picture"), anidb, mal, anilist,
                     None, int(bgm_id) if bgm_id and str(bgm_id).isdigit() else None,
                     zh, None, None))
        # 名字索引
        nms = [(x.get("title"), "aodb_title")]
        for s in (x.get("synonyms") or []):
            nms.append((s, "synonym"))
        if zh:
            nms.append((zh, "zh"))
        for t, src in nms:
            if not t:
                continue
            nn = norm(t)
            if nn:
                names.append((i, t, nn, src))
                n_name += 1
        # 关联作品
        for r in (x.get("relatedAnime") or []):
            m = re.search(r"/(\d+)$", r)
            if m:
                rels.append((i, int(m.group(1))))

    con.executemany("INSERT INTO anime (aid,title,type,episodes,year,season,"
                    "picture,anidb,mal,anilist,tmdb,bangumi,zh_name,meta_json,"
                    "meta_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO name (aid,txt,norm,src) VALUES (?,?,?,?)", names)
    con.executemany("INSERT INTO relate (aid,other) VALUES (?,?)", rels)
    con.commit()
    print("   条目 %d / 名字 %d / 关联 %d" % (len(rows), n_name, len(rels)))

    print("④ 桥接命中：%s" % hit)
    n_bgm = con.execute("SELECT COUNT(*) FROM anime WHERE bangumi IS NOT NULL"
                        ).fetchone()[0]
    n_zh = con.execute("SELECT COUNT(*) FROM anime WHERE zh_name IS NOT NULL"
                       ).fetchone()[0]
    print("   有 bangumi id: %d (%.1f%%)" % (n_bgm, 100 * n_bgm / len(rows)))
    print("   有中文名    : %d (%.1f%%)" % (n_zh, 100 * n_zh / len(rows)))
    con.close()
    print("已写入 %s" % DB)


if __name__ == "__main__":
    build()


def main(args=None):
    """`animediator bgm-aodb` 的入口：把外部标题库并进作业库，供归库层查名字。"""
    return build()


def build_from_bgm(con, bgm_path, verbose=True):
    """用离线 bgm 索引重建名字表 —— **没有 aodb 时的兜底**。

    ════════ 为什么需要这个 ════════
    归库层的离线段查的是 `name` 表（norm → aid → anime.bangumi），
    而这张表原来只从外部 aodb 建。可 1.3 G 的 bgm.db 里
    `sname` 表本来就有 **97 万条别名**（aodb 只有 29 万），
    完全够用 —— 只是名字到 id 的映射绕过了中间那一层。

    实测两种来源在真实 455 个单元上的单值命中率：
        aodb   58.9%
        bgm.db 52.5%
    两边都命中且指向同一条目的占 43.3%，**分歧只有 2 个**。
    所以 bgm.db 单独跑完全可用，只是多约 15% 的单元会走到模型那一步。

    `season` 这一列 bgm.db 没有，留 NULL ——
    它只用于"同名多候选时按季号预筛"，没有就让模型自己判。
    """
    import sqlite3
    src = sqlite3.connect(str(bgm_path))
    if verbose:
        print("  从 %s 重建名字索引（aodb 不存在，用离线索引兜底）…" % bgm_path)
    # anime：subject → 我们的 aid（这里直接把 subject_id 当 aid 用，单一来源不冲突）
    n_s = 0
    con.execute("DELETE FROM anime")
    con.execute("DELETE FROM name")
    rows = []
    for sid, name, name_cn, date in src.execute(
            "SELECT id, name, name_cn, date FROM subject WHERE type=2"):
        year = (date or "")[:4] or None
        rows.append((sid, name, sid, name_cn, year, None, None, None))
        n_s += 1
        if len(rows) >= 5000:
            con.executemany(
                "INSERT OR REPLACE INTO anime (aid,title,bangumi,zh_name,year,"
                "season,episodes,picture) VALUES (?,?,?,?,?,?,?,?)", rows)
            rows = []
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO anime (aid,title,bangumi,zh_name,year,"
            "season,episodes,picture) VALUES (?,?,?,?,?,?,?,?)", rows)
    # name：sname 全量
    n_n = 0
    batch = []
    for sid, txt, nm, src2 in src.execute(
            "SELECT subject_id, txt, norm, src FROM sname"):
        batch.append((sid, txt, nm, src2))
        n_n += 1
        if len(batch) >= 20000:
            con.executemany("INSERT INTO name (aid,txt,norm,src) VALUES (?,?,?,?)", batch)
            batch = []
    if batch:
        con.executemany("INSERT INTO name (aid,txt,norm,src) VALUES (?,?,?,?)", batch)
    con.commit()
    # 集数从 episode 表补（只算正片集）。ATTACH 一下让 SQLite 自己算，
    # 免得在 Python 里搬一百七十万行。
    # ⚠️ 必须先关掉 src 再 ATTACH —— 同一个库被另一个连接持有读事务时，
    #    ATTACH 会报 "database b is locked"。
    src.close()
    try:
        con.execute("ATTACH DATABASE ? AS b", (str(bgm_path),))
        con.execute(
            "UPDATE anime SET episodes=(SELECT COUNT(*) FROM b.episode e "
            "WHERE e.subject_id=anime.aid AND e.type=0 AND e.sort>0)")
        # ⚠️ 先 commit 再 DETACH：SQLite 在 UPDATE 之后还持有 b 的读锁，
        #    直接 DETACH 会报 "database b is locked"，异常一抛就**连提交也跳过了**
        #    —— 表现为"进程里看着有数据，关掉再打开全没了"。
        con.commit()
    except Exception as ex:
        if verbose:
            print("    （集数没补上：%s，不影响归库）" % ex)
    try:
        con.execute("DETACH DATABASE b")
    except Exception:
        pass
    if verbose:
        print("    条目 %d / 名字 %d" % (n_s, n_n))
    return n_s, n_n
