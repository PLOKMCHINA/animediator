"""验证本地 bgm 索引能不能真答问题 —— 身份、中文名、简介、每集中文名。

这是一份**自检**：建完索引之后跑一次，看它到底答不答得上真问题。
不是单元测试（不求通过失败），是给你眼睛看的体检报告。
"""
import sqlite3

from ..config import get as get_cfg
from ..textutil import norm

_con = None


def _c():
    global _con
    if _con is None:
        _con = sqlite3.connect(str(get_cfg().bgm_db))
        _con.row_factory = sqlite3.Row
    return _con


def find(name):
    n = norm(name)
    return _c().execute(
        "SELECT s.id,s.name,s.name_cn,s.date,s.score,s.summary FROM sname n "
        "JOIN subject s ON s.id=n.subject_id WHERE n.norm=? LIMIT 4", (n,)).fetchall()


def main(args=None):
    cfg = get_cfg()
    con = _c()
    print("索引：%s" % cfg.bgm_db)
    print()
    print("═══ ① 精确身份查询（离线，零网络）═══")
    for q in ["葬送的芙莉莲", "Sousou no Frieren", "ソウソウノフリーレン",
              "银魂", "Gintama", "在地下城寻求邂逅是否搞错了什么 第二季",
              "孤独摇滚！", "ぼっち・ざ・ろっく！", "某科学的超电磁炮T"]:
        rows = find(q)
        if rows:
            r = rows[0]
            print("  ✅ %-28s → id=%-7d 「%s」 / %s  %s" % (
                q, r["id"], (r["name_cn"] or "")[:18], (r["name"] or "")[:20],
                ("★%.1f" % r["score"]) if r["score"] else ""))
        else:
            print("  ❌ %-28s 查不到" % q)

    print()
    print("═══ ② 中文简介（离线）═══")
    r = con.execute("SELECT id,name_cn,summary FROM subject WHERE name_cn LIKE ? LIMIT 1",
                    ("%葬送的芙莉莲%",)).fetchone()
    if r:
        print("  《%s》(id=%d)：%s" % (r["name_cn"], r["id"], (r["summary"] or "无")[:150]))

    print()
    print("═══ ③ 每集中文标题（离线）═══")
    for sid, label in [(400602, "葬送的芙莉莲"), (247, "银魂")]:
        eps = con.execute("SELECT sort,name,name_cn,airdate FROM episode "
                          "WHERE subject_id=? ORDER BY sort LIMIT 6", (sid,)).fetchall()
        print("  《%s》id=%d：" % (label, sid))
        if not eps:
            print("     （没有章节数据）")
        for e in eps:
            print("     sort=%-6s 「%s」 / %s" % (
                e["sort"], (e["name_cn"] or "-")[:26], (e["name"] or "")[:26]))

    print()
    print("═══ ④ 覆盖度 ═══")

    def q(s):
        return con.execute(s).fetchone()[0]

    print("  动画条目(type=2)          %d" % q("SELECT COUNT(*) FROM subject WHERE type=2"))
    print("  其中带中文名              %d"
          % q("SELECT COUNT(*) FROM subject WHERE type=2 AND name_cn IS NOT NULL AND name_cn!=''"))
    print("  其中带简介                %d"
          % q("SELECT COUNT(*) FROM subject WHERE type=2 AND summary IS NOT NULL AND summary!=''"))
    print("  有章节的动画条目          %d" % q(
        "SELECT COUNT(DISTINCT subject_id) FROM episode WHERE subject_id IN "
        "(SELECT id FROM subject WHERE type=2)"))
    print("  章节里带中文名            %d / %d" % (
        q("SELECT COUNT(*) FROM episode WHERE name_cn IS NOT NULL AND name_cn!=''"),
        q("SELECT COUNT(*) FROM episode")))
    return 0
