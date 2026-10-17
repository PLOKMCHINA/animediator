"""验证：没有 aodb 时，resolve 的兜底索引能建出来且召回对得上。

预期（之前用真实数据测过的）：
    aodb   58.9% 单值命中
    bgm.db 52.5% 单值命中
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from animediator.config import load
from animediator import db as workdb
from animediator.bgm.aodb import build_from_bgm
from animediator.textutil import norm_query as norm

BRAIN = os.environ.get("AMI_BRAIN", "work/brain_v1.jsonl")
TMP = os.environ.get("AMI_TMP_DB", "/tmp/ami-bootstrap.db")

# 用一份指向真实数据的配置；没有就用当前目录
cfg = load(os.environ.get("AMI_CONFIG"),
           {"work": os.environ.get("AMI_BOOT_WORK", "/tmp/ami-bootstrap-work")})

con = sqlite3.connect(TMP)
con.executescript(workdb.SCHEMA)

t0 = time.time()
n_s, n_n = build_from_bgm(con, cfg.bgm_db)
print("兜底索引建好：条目 %d / 名字 %d，用时 %.1f 秒"
      % (n_s, n_n, time.time() - t0))

# 检查集数补上了没
r = con.execute("SELECT COUNT(*) FROM anime WHERE episodes>0").fetchone()[0]
print("有条目集数的：%d" % r)


def candidates(rec):
    res = rec["result"]
    out, seen = [], set()

    def add(v, src):
        if not v or not str(v).strip():
            return
        n = norm(v)
        if len(n) < 2 or n in seen:
            return
        seen.add(n)
        out.append(str(v).strip())

    add(res.get("title"), "listing")
    for a in (res.get("alt") or []):
        add(a, "listing")
    add(res.get("recall"), "recall")
    return out


rows = [json.loads(l) for l in open(BRAIN, encoding="utf-8")]
single = multi = miss = 0
for rec in rows:
    hit = False
    amb = False
    for name in candidates(rec):
        aids = [r[0] for r in con.execute(
            "SELECT DISTINCT aid FROM name WHERE norm=?", (norm(name),))]
        if len(aids) == 1:
            hit = True
            break
        elif len(aids) > 1:
            amb = True
    if hit:
        single += 1
    elif amb:
        multi += 1
    else:
        miss += 1

tot = len(rows)
print()
print("  单值命中 %4d  (%.1f%%)   ← 之前实测 bgm.db 路线 52.5%%" % (single, 100*single/tot))
print("  多候选   %4d" % multi)
print("  没命中   %4d" % miss)
