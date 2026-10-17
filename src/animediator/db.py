"""作业库（work/animediator.db）—— 中间产物都落在这里，每步幂等。

════════ 表的分工 ════════
  unit        一个「识别单元」= 发布时本来就分好的那一层（一张碟、一个季、一个剧场版）。
              模型一次调用覆盖一个单元，所以单元切得好不好直接决定识别质量。
  ufile       单元里的每个视频文件：路径（相对单元的）、集号、正片/特典。
              **正片/特典的判定位在这一列。**
  bgm_cache   从离线索引里抄过来的条目元数据快照（中文名/原名/简介/海报）。
              抄一份是为了让下游不必每次去 join 那张 1.3G 的 bgm.db。
  llm_meta    离线索引里压根没有的条目，由模型凭知识补的中文信息。

════════ 为什么集号允许是 NULL ════════
原来落点算不出来时整批作废（"全或无"）。实测这会让**一个重复集号**
就把 200 多集连坐降级成特典。现在改成逐文件落点：落不上的只牺牲它自己。
所以 `ep IS NULL` 是**正常状态**，不是错误 —— 它表示"这个文件确实
算不出集号"，诚实地记下来，好过瞎填一个。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS unit (
  uid      INTEGER PRIMARY KEY,
  disk     TEXT,
  unit     TEXT UNIQUE,
  aid      INTEGER,
  bgm_id   INTEGER,
  src      TEXT,
  conf     TEXT,
  matched  TEXT,
  name_cn  TEXT,
  name_ja  TEXT,
  year     TEXT,
  date     TEXT,
  summary  TEXT,
  poster   TEXT,
  season   INTEGER,
  part     TEXT,
  dir_kind TEXT,          -- TV / 剧场版 / OVA / SP …
  has_main INTEGER,       -- 这个单元里有没有正片（模型的表态）
  n_files  INTEGER,
  n_ep     INTEGER,
  reading  TEXT,          -- 模型对目录名的读法
  notes    TEXT,
  problem  TEXT
);

CREATE TABLE IF NOT EXISTS ufile (
  uid  INTEGER,
  i    INTEGER,           -- 单元内的序号，稳定，别改
  path TEXT,              -- **相对单元目录**的子路径（不是全路径）
  ep   INTEGER,           -- 算不出来就是 NULL，正常
  kind TEXT,              -- 正片 / 特典
  what TEXT               -- 特典说明（NCOP / PV / Menu …）
);

-- ── 名字索引（归库层用）：名字 → 条目 ──
-- ⚠️ 这三张表的数据来自外部标题库（aodb）。**没有也能跑** ——
--    resolve 会在表为空时自动用 bgm.db 的 97 万条别名兜底重建。
CREATE TABLE IF NOT EXISTS anime (
  aid      INTEGER PRIMARY KEY,
  title    TEXT,
  bangumi  INTEGER,     -- 番组计划 subject id
  zh_name  TEXT,
  year     TEXT,
  season   INTEGER,
  episodes INTEGER,
  picture  TEXT
);
CREATE TABLE IF NOT EXISTS name (
  aid  INTEGER,
  txt  TEXT,
  norm TEXT,
  src  TEXT
);
CREATE TABLE IF NOT EXISTS relate (
  aid   INTEGER,
  other INTEGER
);
CREATE INDEX IF NOT EXISTS idx_name_norm    ON name(norm);
CREATE INDEX IF NOT EXISTS idx_name_aid     ON name(aid);
CREATE INDEX IF NOT EXISTS idx_anime_bangumi ON anime(bangumi);

CREATE TABLE IF NOT EXISTS bgm_cache (
  bgm_id  INTEGER PRIMARY KEY,
  name_cn TEXT,
  name_ja TEXT,
  date    TEXT,
  summary TEXT,
  poster  TEXT
);

CREATE TABLE IF NOT EXISTS llm_meta (
  unit    TEXT PRIMARY KEY,
  name_cn TEXT,
  name_ja TEXT,
  summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_ufile_uid ON ufile(uid);
CREATE INDEX IF NOT EXISTS idx_unit_bgm  ON unit(bgm_id);
CREATE INDEX IF NOT EXISTS idx_ufile_ep  ON ufile(uid, ep);
"""


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init(path) -> sqlite3.Connection:
    con = connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def units_with_files(con) -> list[dict]:
    """把 unit + 它的文件一次读出来。下游模块都从这里取，别各写一份 join。"""
    units = [dict(r) for r in con.execute("SELECT * FROM unit ORDER BY uid")]
    by_uid = {}
    for r in con.execute("SELECT uid,i,path,ep,kind,what FROM ufile ORDER BY uid,i"):
        by_uid.setdefault(r["uid"], []).append(dict(r))
    for u in units:
        u["files"] = by_uid.get(u["uid"], [])
    return units


def cache_put(con, bgm_id, name_cn, name_ja, date, summary, poster) -> None:
    con.execute(
        "INSERT OR REPLACE INTO bgm_cache (bgm_id,name_cn,name_ja,date,summary,poster)"
        " VALUES (?,?,?,?,?,?)",
        (bgm_id, name_cn, name_ja, date, summary, poster))


def cache_get(con, bgm_id):
    r = con.execute("SELECT * FROM bgm_cache WHERE bgm_id=?", (bgm_id,)).fetchone()
    return dict(r) if r else None
