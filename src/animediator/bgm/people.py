"""把 person / character / 关联表解析进本地索引 —— 班底、声优、角色。

════════ 为什么需要 ════════
Jellyfin 的海报墙点进一部番，下面的「演职人员」区块要有东西：
    监督/脚本/音乐 → `<director>` `<credits>` `<studio>`
    角色 + 谁配的音 → `<actor><name>声优</name><role>角色</role></actor>`
这些从 dump 的 6 张关联表来：

    subject-persons      条目 ↔ 人物（position = 职位编号）
    subject-characters   条目 ↔ 角色（type = 主角/配角/客串）
    person-characters    人物 ↔ 角色（**谁配的音**）
    subject-relations    条目 ↔ 条目（前传/续作/同系列）
    person               人物本体（career = 声优/artist…）
    character            角色本体

⚠️ 关于从 infobox 抠中文名：
   `infobox` 是原始 wiki 字符串，中文名长这样 `|简体中文名= 鲁路修·兰佩路基`。
   这里做的是**读一个带标签的字段**（机械取值），不是让代码替模型做判断 ——
   同一套规矩下允许的操作。而且原始 infobox **整段原样保留**在表里，
   将来要抠别的字段不用重下 445MB。
"""
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

from ..config import get as get_cfg
from ..textutil import norm_query as norm

# 在 main() 里从配置取
DUMP = None
BGM = None

DDL = """
DROP TABLE IF EXISTS person;
DROP TABLE IF EXISTS character;
DROP TABLE IF EXISTS subject_person;
DROP TABLE IF EXISTS subject_character;
DROP TABLE IF EXISTS person_character;
DROP TABLE IF EXISTS subject_relation;
DROP TABLE IF EXISTS pname;
DROP TABLE IF EXISTS cname;
CREATE TABLE person (
  id INTEGER PRIMARY KEY, name TEXT, name_cn TEXT,
  type INTEGER, career TEXT, summary TEXT, infobox TEXT
);
CREATE TABLE character (
  id INTEGER PRIMARY KEY, name TEXT, name_cn TEXT,
  role INTEGER, summary TEXT, infobox TEXT
);
CREATE TABLE subject_person (
  subject_id INTEGER, person_id INTEGER, position INTEGER, appear_eps TEXT
);
CREATE TABLE subject_character (
  subject_id INTEGER, character_id INTEGER, type INTEGER, ord INTEGER
);
CREATE TABLE person_character (
  person_id INTEGER, character_id INTEGER, subject_id INTEGER, type INTEGER
);
CREATE TABLE subject_relation (
  subject_id INTEGER, relation_type INTEGER, related_id INTEGER, ord INTEGER
);
CREATE TABLE pname (person_id INTEGER, txt TEXT, norm TEXT);
CREATE TABLE cname (character_id INTEGER, txt TEXT, norm TEXT);
"""

def cn_from_infobox(ib):
    """从 wiki infobox 里读 `简体中文名` 字段。读不到就回 None，不猜。"""
    if not ib:
        return None
    for key in ("简体中文名", "中文名"):
        m = re.search(r"\|\s*%s\s*=\s*([^|\n\r]+)" % key, ib)
        if m:
            v = m.group(1).strip()
            if v and v not in ("{", "}"):
                return v
    return None


def lines(zf, name):
    with zf.open(name) as raw:
        for ln in io.TextIOWrapper(raw, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                yield json.loads(ln)


def main(args=None):
    cfg = get_cfg()
    global DUMP, BGM
    DUMP = str(cfg.dump)
    BGM = str(cfg.bgm_db)
    zf = zipfile.ZipFile(DUMP)
    con = sqlite3.connect(BGM)
    t0 = time.time()
    con.executescript(DDL)
    con.commit()

    # ── person ──
    n, rows, nms = 0, [], []
    for d in lines(zf, "person.jsonlines"):
        if d.get("id") is None:
            continue
        cn = cn_from_infobox(d.get("infobox"))
        rows.append((d["id"], d.get("name"), cn, d.get("type"),
                     json.dumps(d.get("career") or [], ensure_ascii=False),
                     d.get("summary"), d.get("infobox")))
        for v in (d.get("name"), cn):
            if v and len(v.strip()) >= 2:
                nms.append((d["id"], v.strip(), norm(v)))
        n += 1
        if len(rows) >= 5000:
            con.executemany("INSERT OR REPLACE INTO person VALUES (?,?,?,?,?,?,?)", rows)
            con.executemany("INSERT INTO pname VALUES (?,?,?)", nms)
            rows, nms = [], []
    if rows:
        con.executemany("INSERT OR REPLACE INTO person VALUES (?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO pname VALUES (?,?,?)", nms)
    con.commit()
    print("person     %d 条  %.0fs" % (n, time.time() - t0), flush=True)

    # ── character ──
    n, rows, nms = 0, [], []
    for d in lines(zf, "character.jsonlines"):
        if d.get("id") is None:
            continue
        cn = cn_from_infobox(d.get("infobox"))
        rows.append((d["id"], d.get("name"), cn, d.get("role"),
                     d.get("summary"), d.get("infobox")))
        for v in (d.get("name"), cn):
            if v and len(v.strip()) >= 2:
                nms.append((d["id"], v.strip(), norm(v)))
        n += 1
        if len(rows) >= 5000:
            con.executemany("INSERT OR REPLACE INTO character VALUES (?,?,?,?,?,?)", rows)
            con.executemany("INSERT INTO cname VALUES (?,?,?)", nms)
            rows, nms = [], []
    if rows:
        con.executemany("INSERT OR REPLACE INTO character VALUES (?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO cname VALUES (?,?,?)", nms)
    con.commit()
    print("character  %d 条  %.0fs" % (n, time.time() - t0), flush=True)

    # ── 关联表（结构简单，直接灌）──
    for fn, tbl, cols in [
        ("subject-persons.jsonlines", "subject_person", ("subject_id", "person_id", "position")),
        ("subject-characters.jsonlines", "subject_character", ("subject_id", "character_id", "type")),
    ]:
        n, rows = 0, []
        extra = {"subject_person": "appear_eps", "subject_character": "ord"}[tbl]
        for d in lines(zf, fn):
            r = tuple(d.get(c) for c in cols)
            v = d.get(extra)
            r = r + (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v,)
            rows.append(r)
            n += 1
            if len(rows) >= 20000:
                con.executemany("INSERT INTO %s VALUES (?,?,?,?)" % tbl, rows)
                rows = []
        if rows:
            con.executemany("INSERT INTO %s VALUES (?,?,?,?)" % tbl, rows)
        con.commit()
        print("%-18s %d 条  %.0fs" % (tbl, n, time.time() - t0), flush=True)

    for fn, tbl in [("person-characters.jsonlines", "person_character")]:
        n, rows = 0, []
        for d in lines(zf, fn):
            rows.append((d.get("person_id"), d.get("character_id"),
                         d.get("subject_id"), d.get("type")))
            n += 1
            if len(rows) >= 20000:
                con.executemany("INSERT INTO %s VALUES (?,?,?,?)" % tbl, rows)
                rows = []
        if rows:
            con.executemany("INSERT INTO %s VALUES (?,?,?,?)" % tbl, rows)
        con.commit()
        print("%-18s %d 条  %.0fs" % (tbl, n, time.time() - t0), flush=True)

    n, rows = 0, []
    for d in lines(zf, "subject-relations.jsonlines"):
        rows.append((d.get("subject_id"), d.get("relation_type"),
                     d.get("related_subject_id"), d.get("order")))
        n += 1
        if len(rows) >= 20000:
            con.executemany("INSERT INTO subject_relation VALUES (?,?,?,?)", rows)
            rows = []
    if rows:
        con.executemany("INSERT INTO subject_relation VALUES (?,?,?,?)", rows)
    con.commit()
    print("%-18s %d 条  %.0fs" % ("subject_relation", n, time.time() - t0), flush=True)

    print("建索引...", flush=True)
    for s in [
        "CREATE INDEX idx_sp_sub ON subject_person(subject_id)",
        "CREATE INDEX idx_sp_per ON subject_person(person_id)",
        "CREATE INDEX idx_sc_sub ON subject_character(subject_id)",
        "CREATE INDEX idx_sc_chr ON subject_character(character_id)",
        "CREATE INDEX idx_pc_sub ON person_character(subject_id)",
        "CREATE INDEX idx_pc_per ON person_character(person_id)",
        "CREATE INDEX idx_pc_chr ON person_character(character_id)",
        "CREATE INDEX idx_sr_sub ON subject_relation(subject_id)",
        "CREATE INDEX idx_pname_n ON pname(norm)",
        "CREATE INDEX idx_cname_n ON cname(norm)",
    ]:
        con.execute(s)
    con.execute("ANALYZE")
    con.commit()

    # ── 职位编号分布（待查常量映射）──
    print("\nsubject_person 里出现最多的 position 编号：")
    for p, c in con.execute("SELECT position,COUNT(*) FROM subject_person "
                            "GROUP BY position ORDER BY 2 DESC LIMIT 14"):
        print("   position=%-6s %d 次" % (p, c))
    print("\n关系编号分布：")
    for p, c in con.execute("SELECT relation_type,COUNT(*) FROM subject_relation "
                            "GROUP BY relation_type ORDER BY 2 DESC LIMIT 10"):
        print("   relation_type=%-6s %d 次" % (p, c))

    con.close()
    print("\nbgm.db 现在 %.1f MB，总用时 %.0f 秒" % (
        os.path.getsize(BGM) / 1048576, time.time() - t0))

