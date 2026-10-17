"""对比两种名字索引的召回：aodb 表 vs bgm.db 的 sname。

要回答的问题：resolve 的离线段（纯 SQL，不调模型）如果改查 bgm.db，
命中率是变好、变差、还是持平？
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from animediator.textutil import norm_query as norm

# 路径从环境变量取，默认按标准目录结构猜
WORK = os.environ.get("AMI_WORK", "work") + "/animediator.db"
BGM = os.environ.get("AMI_BGMDb", "data/bgm.db")
BRAIN = os.environ.get("AMI_BRAIN", "work/brain_v1.jsonl")


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
        out.append((str(v).strip(), src))

    add(res.get("title"), "listing")
    for a in (res.get("alt") or []):
        add(a, "listing")
    add(res.get("recall"), "recall")
    return out


rows = [json.loads(l) for l in open(BRAIN, encoding="utf-8")]
a = sqlite3.connect(WORK)
b = sqlite3.connect(BGM)

stat = {"both_same": 0, "both_diff": 0, "aodb_only": 0, "bgm_only": 0,
        "neither": 0, "aodb_amb": 0, "bgm_amb": 0}
examples = []

for rec in rows:
    aodb_hit, bgm_hit = None, None
    aodb_multi = bgm_multi = False
    for name, src in candidates(rec):
        n = norm(name)
        # ── aodb 路线：name.norm → aid → anime.bangumi ──
        if aodb_hit is None:
            aids = [r[0] for r in a.execute(
                "SELECT DISTINCT aid FROM name WHERE norm=?", (n,))]
            if len(aids) == 1:
                r = a.execute("SELECT bangumi FROM anime WHERE aid=?", (aids[0],)).fetchone()
                if r and r[0]:
                    aodb_hit = r[0]
            elif len(aids) > 1:
                aodb_multi = True
        # ── bgm 路线：sname.norm → subject_id ──
        if bgm_hit is None:
            sids = [r[0] for r in b.execute(
                "SELECT DISTINCT subject_id FROM sname WHERE norm=?", (n,))]
            if len(sids) == 1:
                r = b.execute("SELECT type FROM subject WHERE id=?", (sids[0],)).fetchone()
                if r and r[0] == 2:          # 只要番剧
                    bgm_hit = sids[0]
            elif len(sids) > 1:
                bgm_multi = True
        if aodb_hit and bgm_hit:
            break

    if aodb_hit and bgm_hit:
        if aodb_hit == bgm_hit:
            stat["both_same"] += 1
        else:
            stat["both_diff"] += 1
            if len(examples) < 12:
                examples.append(("分歧", rec["unit"][-50:], aodb_hit, bgm_hit))
    elif aodb_hit:
        stat["aodb_only"] += 1
        if len(examples) < 12:
            examples.append(("只aodb", rec["unit"][-50:], aodb_hit, None))
    elif bgm_hit:
        stat["bgm_only"] += 1
        if len(examples) < 12:
            examples.append(("只bgm", rec["unit"][-50:], None, bgm_hit))
    else:
        stat["neither"] += 1
        if aodb_multi:
            stat["aodb_amb"] += 1
        if bgm_multi:
            stat["bgm_amb"] += 1

tot = len(rows)
print("单元总数 %d" % tot)
print()
print("  两边都命中、指向同一条目 : %4d  (%.1f%%)" % (stat["both_same"], 100*stat["both_same"]/tot))
print("  两边都命中、但指向不同   : %4d" % stat["both_diff"])
print("  只有 aodb 命中           : %4d" % stat["aodb_only"])
print("  只有 bgm.db 命中         : %4d" % stat["bgm_only"])
print("  两边都没命中             : %4d  (其中 aodb 多候选 %d / bgm 多候选 %d)"
      % (stat["neither"], stat["aodb_amb"], stat["bgm_amb"]))
print()
print("  ⟹ 单值命中率  aodb %.1f%%   bgm.db %.1f%%"
      % (100*(stat["both_same"]+stat["both_diff"]+stat["aodb_only"])/tot,
         100*(stat["both_same"]+stat["both_diff"]+stat["bgm_only"])/tot))
print()
if examples:
    print("样例：")
    for tag, u, x, y in examples:
        print("  [%s] %-50s aodb=%-8s bgm=%s" % (tag, u, x, y))
