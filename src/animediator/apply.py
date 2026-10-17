"""把重新归库的结果写进 unit 表。**带备份，可回滚。**

前提：work/reresolve3.jsonl 是 reresolve_search (v2) 的产物，每行
      {uid, unit, old, new}；`new` 为空表示模型无法确定（保留原判）。

⚠️ 只改 unit.bgm_id 一列，**不动任何文件**。
   这是整个工程里最重要的安全网 —— 它只改 unit.bgm_id 一列，不动任何文件。
   备份：把改动前的全部 (uid, unit, bgm_id) 写到 work/unit_bgm_backup.json
   （json 备份 + 整库快照，备份已存在就跳过；`--dry-run` 时两者都不做）。
"""
import json
import os
import shutil
import sqlite3
import sys
import time

from .config import get as get_cfg, FLAGS

cfg = get_cfg()
DB = cfg.db
SRC = cfg.w("reresolve3.jsonl")
BK = cfg.w("unit_bgm_backup.json")


def main(args=None):
    if FLAGS.dry_run:
        rows = [json.loads(l) for l in open(SRC, encoding="utf-8")]
        key = {}
        for r in rows:
            key[(r["uid"], r["unit"])] = r
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        changed = 0
        for r in con.execute("SELECT uid, unit, bgm_id FROM unit"):
            d = key.get((r["uid"], r["unit"]))
            if not d:
                continue
            new = d.get("new")
            if not new:
                continue
            if int(new) == int(r["bgm_id"] or 0):
                continue
            changed += 1
        con.close()
        print("[dry-run] 将改判 %d 行（不备份、不快照、不改库）" % changed)
        return 0

    rows = [json.loads(l) for l in open(SRC, encoding="utf-8")]
    key = {}
    for r in rows:
        key[(r["uid"], r["unit"])] = r

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ── ① 备份全表 ──
    if not os.path.exists(BK):
        data = [dict(r) for r in con.execute(
            "SELECT uid, unit, disk, bgm_id, aid, name_cn, season FROM unit")]
        json.dump(data, open(BK, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("备份 %d 行 → %s" % (len(data), BK))
    else:
        print("备份已存在，跳过（%s）" % BK)
    # 顺手留一份整库快照
    snap = "%s.bak-%s" % (DB, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(DB, snap)
    print("整库快照 → %s" % snap)

    # ── ② 应用 ──
    changed, kept, unknown, notfound = 0, 0, 0, 0
    detail = []
    for r in con.execute("SELECT uid, unit, bgm_id FROM unit"):
        k = (r["uid"], r["unit"])
        d = key.get(k)
        if not d:
            notfound += 1
            continue
        new = d.get("new")
        if not new:
            unknown += 1
            continue
        if int(new) == int(r["bgm_id"] or 0):
            kept += 1
            continue
        con.execute("UPDATE unit SET bgm_id=? WHERE uid=? AND unit=?",
                    (int(new), r["uid"], r["unit"]))
        changed += 1
        detail.append((r["unit"], r["bgm_id"], int(new)))
    con.commit()

    print()
    print("改判写入 %d 行" % changed)
    print("维持原判 %d 行" % kept)
    print("模型未定 %d 行（保留原判）" % unknown)
    print("不在结果里 %d 行" % notfound)

    tot = con.execute("SELECT COUNT(DISTINCT bgm_id) FROM unit WHERE bgm_id IS NOT NULL").fetchone()[0]
    nu = con.execute("SELECT COUNT(*) FROM unit WHERE bgm_id IS NULL").fetchone()[0]
    print()
    print("现在库里有 %d 个不同 bgm 条目（原 121），无身份的 unit %d 个" % (tot, nu))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
