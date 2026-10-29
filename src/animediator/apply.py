"""把重新归库的结果写进 unit 表。**带备份，可回滚。**

前提：work/reresolve3.jsonl 是 reresolve_search (v2) 的产物，每行
      {uid, unit, old, new}；`new` 为空表示模型无法确定（保留原判）。

⚠️ 只改 unit.bgm_id 一列，**不动任何文件**。
   这是整个工程里最重要的安全网 —— 它只改 unit.bgm_id 一列，不动任何文件。
   备份：把改动前的全部 (uid, unit, bgm_id) 写到 work/unit_bgm_backup.json
   （json 备份 + 整库快照，备份已存在就跳过；`--dry-run` 时两者都不做）。
   回滚用 `animediator lib undo`。
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


def _read_rows():
    """读复核结果。文件不在时说明该怎么办，不抛堆栈。"""
    if not SRC.exists():
        raise SystemExit(
            "缺少 %s\n"
            "  这是复核结果，需要先跑 `animediator recheck` 生成。\n"
            "  如果已经跑过别的流程，确认 --config 指向的是同一个工程目录。"
            % SRC)
    rows = []
    with open(SRC, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise SystemExit("%s 第 %d 行不是合法 JSON，文件可能没写完" % (SRC, i))
    return rows


def main(args=None):
    if FLAGS.dry_run:
        rows = _read_rows()
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

    rows = _read_rows()
    key = {}
    for r in rows:
        key[(r["uid"], r["unit"])] = r

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ── ① 备份全表 ──
    # 每次操作都留一份带时间戳的，保留的是「这次改动之前」的状态。
    # 曾经只在文件不存在时写，结果那份永远是第一次动库之前的样子，
    # lib undo 拿去还原等于退到最初，撤销不了上一次。
    ts = time.strftime("%Y%m%d-%H%M%S")
    bk = cfg.w("unit_bgm_backup.%s.json" % ts)
    data = [dict(r) for r in con.execute(
        "SELECT uid, unit, disk, bgm_id, aid, name_cn, season FROM unit")]
    json.dump(data, open(bk, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("备份 %d 行 → %s" % (len(data), bk.name))
    # 顺手留一份整库快照（命名和 assign 一致，lib undo 才认得出来）
    snap = "%s.%s.bak" % (DB, ts)
    n = 2
    while os.path.exists(snap):
        snap = "%s.%s-%d.bak" % (DB, ts, n)
        n += 1
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
