"""把 bangumi/common 的常量映射表灌进本地索引。

`subject-persons.position` 是数字编号（2004 是什么？），
`subject-relations.relation_type` 也是编号。
映射在 `bangumi/common`（仓库真实结构，实地看过）：
    subject_staffs.yml     define.types.{anime,game,book,music,real} → {编号: {en,cn,jp,categories}}
    subject_relations.yml  define.types.{anime,book,music,game}      → {编号: {en,cn,jp,desc}}
                           relations 里还有一份按 subject type 分组的完整副本
必须本地化 —— 否则渲染班底时全是裸数字。

⚠️ 职位编号**按作品类型分域**：动画的 2004 和游戏的 2004 含义不保证一样，
   所以表里带 scope 列，查的时候必须带 `scope='anime'`。
"""
import os
import sqlite3
import sys

from ..config import get as get_cfg

# yaml 只用来读这两张**外部常量表**（bangumi/common 仓库里给的 staffs / relations）。
# 它是本工程唯一的第三方依赖，所以做成可选：没有就给出明确的获取办法，
# 而不是在 import 阶段就把整个包拖死。
try:
    import yaml
except ModuleNotFoundError:            # pragma: no cover
    yaml = None

YAML_HINT = (
    "解析 staffs/relations 常量表需要 PyYAML（pip install pyyaml）。\n"
    "  这两张表来自 bangumi/common 仓库的 yml，用它把 Bangumi 的职位/关联编号\n"
    "  翻成中文名。不装也不影响主链 —— 只是索引里这两张对照表是空的。")

# 在 main() 里从配置取：data/staffs.yml 与 data/relations.yml
STAFFS = None
RELS = None
BGM = None


def load_staffs():
    d = yaml.safe_load(open(STAFFS, encoding="utf-8"))
    out = {}
    for scope, items in (d.get("define", {}).get("types") or {}).items():
        if not isinstance(items, dict):
            continue
        for k, v in items.items():
            if not isinstance(v, dict):
                continue
            try:
                key = (scope, int(k))
            except (ValueError, TypeError):
                continue
            cats = v.get("categories") or []
            catcn = ",".join(c.get("cn", "") for c in cats if isinstance(c, dict))
            caten = ",".join(c.get("en", "") for c in cats if isinstance(c, dict))
            out[key] = (v.get("cn"), v.get("en"), v.get("jp"), catcn, caten)
    return out


def load_rels():
    """返回 {(subject_type, relation_id): (cn,en,jp,desc)}"""
    d = yaml.safe_load(open(RELS, encoding="utf-8"))
    out = {}
    for stype, items in (d.get("define", {}).get("types") or {}).items():
        if not isinstance(items, dict):
            continue
        for k, v in items.items():
            if not isinstance(v, dict):
                continue
            try:
                out[(stype, int(k))] = (v.get("cn"), v.get("en"), v.get("jp"), v.get("desc"))
            except (ValueError, TypeError):
                pass
    return out


def main(args=None):
    if yaml is None:
        print(YAML_HINT)
        return 2
    global STAFFS, RELS, BGM
    cfg = get_cfg()
    b = cfg.data_dir / "staffs.yml"
    r = cfg.data_dir / "relations.yml"
    if not (b.exists() and r.exists()):
        print("常量表不在 %s 或 %s" % (b, r))
        print("  从 https://github.com/bangumi/common 取 subject_staffs.yml / "
              "subject_relations.yml，")
        print("  放进 %s 并改名成 staffs.yml / relations.yml" % cfg.data_dir)
        return 2
    STAFFS, RELS, BGM = str(b), str(r), str(cfg.bgm_db)
    con = sqlite3.connect(BGM)
    con.executescript("""
        DROP TABLE IF EXISTS staff_name;
        DROP TABLE IF EXISTS relation_name;
        CREATE TABLE staff_name (
          scope TEXT, id INTEGER, cn TEXT, en TEXT, jp TEXT, cat TEXT, cat_en TEXT
        );
        CREATE TABLE relation_name (
          scope TEXT, id INTEGER, cn TEXT, en TEXT, jp TEXT, descr TEXT
        );
    """)

    st = load_staffs()
    con.executemany("INSERT INTO staff_name VALUES (?,?,?,?,?,?,?)",
                    [(s, i, cn, en, jp, cat, cate) for (s, i), (cn, en, jp, cat, cate) in st.items()])
    print("职位映射 %d 条（按类型分域：%s）" % (
        len(st), ", ".join("%s=%d" % (s, sum(1 for k in st if k[0] == s))
                           for s in sorted({k[0] for k in st}))))

    rl = load_rels()
    con.executemany("INSERT INTO relation_name VALUES (?,?,?,?,?,?)",
                    [(s, i, cn, en, jp, de) for (s, i), (cn, en, jp, de) in rl.items()])
    print("关系映射 %d 条" % len(rl))
    con.commit()

    print("\n抽查 —— 我们库里真出现过的 position 编号：")
    ok = bad = 0
    for pid, c in con.execute("""SELECT position, COUNT(*) FROM subject_person
                                 GROUP BY position ORDER BY 2 DESC LIMIT 20"""):
        r = con.execute("SELECT cn,en,cat FROM staff_name WHERE id=? AND scope='anime'",
                        (pid,)).fetchone()
        if r:
            ok += 1
            print("   %-6s %-8d → %-12s %s  [%s]" % (pid, c, r[0] or "-", (r[1] or "")[:34], r[2]))
        else:
            bad += 1
            print("   %-6s %-8d → ❌ 无映射" % (pid, c))

    # 全库映射覆盖率
    tot = con.execute("SELECT COUNT(*) FROM subject_person").fetchone()[0]
    have = con.execute("SELECT COUNT(*) FROM subject_person sp WHERE EXISTS "
                       "(SELECT 1 FROM staff_name s WHERE s.id=sp.position AND s.scope='anime')"
                       ).fetchone()[0]
    print("\n全库 subject_person %d 条，职位能翻译的 %d 条（%.1f%%）" % (
        tot, have, 100.0 * have / max(1, tot)))

    print("\n关系编号抽查：")
    for rid, c in con.execute("""SELECT relation_type, COUNT(*) FROM subject_relation
                                 GROUP BY relation_type ORDER BY 2 DESC LIMIT 12"""):
        r = con.execute("SELECT cn,en FROM relation_name WHERE id=? AND scope='anime'",
                        (rid,)).fetchone()
        print("   %-6s %-8d → %s" % (rid, c, ("%s / %s" % r) if r else "❌ 无映射"))
    con.close()

