"""把 aodb 的罗马音/别名名字键并进本地 bgm 索引。

════════ 为什么必须并 ════════
官方 dump 只有 `name`（日文原名）+ `name_cn`（中文名），**没有罗马音**。
可我们的文件名全是罗马音 —— 实测 `Sousou no Frieren` / `Gintama` 在纯 dump 索引里
**一条都查不到**。而手头这份 aodb 有 243,185 条 synonym（罗马音/英文/各种别名），
并且 8511 个条目带 bangumi id，正好当桥。

并法：
    对每条 `anime.bangumi` 非空的记录 → 把它的所有 `name` 名字键
    以 `subject_id = int(bangumi)` 写进 bgm.db 的 `sname`，src 标成 'aodb'。
    这样罗马音、英文名、别名全部能在**本地**命中同一个 bgm id。

⚠️ 只并**有 bgm id 的**（8511 条）。没有的不并 —— 并了也指不到 bgm 条目，
   还会污染名字空间造成错配。
"""
import os
import re
import sqlite3
import sys

from ..config import get as get_cfg
from ..textutil import norm_query as norm

# 在 main() 里从配置取
BGM = None
AODB = None

def main(args=None):
    global BGM, AODB
    cfg = get_cfg()
    BGM = str(cfg.bgm_db)
    AODB = str(cfg.db)          # 作业库：sname 从它的 name 表并过去
    a = sqlite3.connect(AODB)
    b = sqlite3.connect(BGM)
    # 已经有 aodb 来的就清掉，保证幂等
    b.execute("DELETE FROM sname WHERE src='aodb'")
    b.commit()

    ids = {}
    for aid, bg in a.execute("SELECT aid, bangumi FROM anime "
                             "WHERE bangumi IS NOT NULL AND bangumi != ''"):
        try:
            ids[aid] = int(str(bg).strip())
        except (ValueError, TypeError):
            continue
    print("aodb 里带 bgm id 的条目 %d" % len(ids))

    rows, n = [], 0
    for aid, txt, src in a.execute("SELECT aid, txt, src FROM name"):
        bid = ids.get(aid)
        if not bid or not txt:
            continue
        t = txt.strip()
        if len(t) < 2:
            continue
        rows.append((bid, t, norm(t), "aodb"))
        n += 1
        if len(rows) >= 20000:
            b.executemany("INSERT INTO sname VALUES (?,?,?,?)", rows)
            rows = []
    if rows:
        b.executemany("INSERT INTO sname VALUES (?,?,?,?)", rows)
    b.commit()
    print("并入名字键 %d 条" % n)

    # 重建名字索引
    b.execute("DROP INDEX IF EXISTS idx_sname_norm")
    b.execute("CREATE INDEX idx_sname_norm ON sname(norm)")
    b.execute("ANALYZE")
    b.commit()

    tot = b.execute("SELECT COUNT(*) FROM sname").fetchone()[0]
    print("sname 现在共 %d 条（%.1f MB）" % (tot, os.path.getsize(BGM) / 1048576))

    # ── 验证：刚才查不到的罗马音 ──
    print("\n复验罗马音查询：")
    for q in ["Sousou no Frieren", "Gintama", "Oshi no Ko", "DanMachi",
              "Bocchi the Rock", "Shingeki no Kyojin", "Horimiya",
              "Frieren: Beyond Journey's End", "Spy x Family", "Kimi no Na wa"]:
        rows2 = b.execute(
            "SELECT s.id,s.type,s.name_cn,s.name FROM sname n JOIN subject s ON s.id=n.subject_id "
            "WHERE n.norm=? ORDER BY (s.type=2) DESC LIMIT 2", (norm(q),)).fetchall()
        if rows2:
            r = rows2[0]
            print("  ✅ %-30s → id=%-7s type=%s 「%s」/ %s" % (
                q, r[0], r[1], (r[2] or "")[:16], (r[3] or "")[:18]))
        else:
            print("  ❌ %-30s 仍查不到" % q)
    b.close()
    a.close()

