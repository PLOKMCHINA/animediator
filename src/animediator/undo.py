"""从备份回滚。

apply 和 assign 动库之前都留了两样东西：整库快照、unit 表的 bgm_id 全表备份。
但一直没有回滚入口 —— 备份留着用不上，等于没做。这里把入口补上。

    animediator lib undo --list              看有哪些备份、都是什么时候的
    animediator lib undo --snapshot --latest 整库回到最近一次快照
    animediator lib undo --snapshot <文件>   整库回到指定快照
    animediator lib undo --bgm               只把 bgm_id 一列按备份还原

整库回滚会丢掉快照之后的全部改动，所以回滚前会先把当前库另存一份 ——
滚错了还能滚回来。--bgm 只动 bgm_id 一列，比整库回滚轻，适合「只有归属改了、
别的字段想留着」的情况。

认两种快照命名：现在写的 `<库>.20261003-221500.bak`，和早期 apply 写的
`<库>.bak-20261003-221500`。旧的照样能用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .config import FLAGS, get as get_cfg

# 快照文件名里的时间戳，兼容两种写法
_STAMP = re.compile(r"(?:\.bak-|(?<!\d)\.)(\d{8}-\d{6})(?:\.bak)?$")


def _snapshots(cfg):
    """列出 work 下的整库快照，按时间从新到旧。"""
    out = []
    for p in cfg.work.iterdir() if cfg.work.exists() else []:
        n = p.name
        base = os.path.basename(str(cfg.db))
        if not n.startswith(base):
            continue
        if not (n.endswith(".bak") or ".bak-" in n):
            continue
        if not p.is_file():
            continue
        m = _STAMP.search(n)
        stamp = m.group(1) if m else ""
        out.append({"path": str(p), "name": n, "stamp": stamp,
                    "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    out.sort(key=lambda x: (x["stamp"], x["mtime"]), reverse=True)
    return out


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def _bgm_backups(cfg):
    """所有 unit 表的 bgm_id 备份，按时间从新到旧。

    现在的写法是 unit_bgm_backup.<时间戳>.json，每次操作留一份；
    早期只有不帶时间戳的一份，照样认。
    """
    out = []
    for p in (cfg.work.iterdir() if cfg.work.exists() else []):
        n = p.name
        if not (n.startswith("unit_bgm_backup") and n.endswith(".json")):
            continue
        m = _STAMP.search(n.replace(".json", ""))
        stamp = m.group(1) if m else ""
        out.append({"path": str(p), "name": n, "stamp": stamp,
                    "mtime": p.stat().st_mtime})
    out.sort(key=lambda x: (x["stamp"], x["mtime"]), reverse=True)
    return out


def _show(rows, cfg):
    print("整库快照：")
    if not rows:
        print("  （没有）")
    for i, r in enumerate(rows):
        when = r["stamp"] or time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(r["mtime"]))
        print("  [%d] %s  %s  %s" % (
            i, when, _fmt_size(r["size"]),
            "← 最近" if i == 0 else ""))
        print("      %s" % r["name"])
    print()
    bks = _bgm_backups(cfg)
    print("bgm_id 全表备份：")
    if not bks:
        print("  （没有）")
    for i, b in enumerate(bks):
        try:
            n = len(json.loads(open(b["path"], encoding="utf-8").read()))
            info = "%d 行" % n
        except (OSError, ValueError):
            info = "读不出来"
        when = b["stamp"] or time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(b["mtime"]))
        print("  [%d] %s  %s%s" % (i, when, info,
                                  "  ← 最近" if i == 0 else ""))
        print("      %s" % b["name"])
    return 0


def _integrity(path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        got = con.execute("PRAGMA integrity_check").fetchone()
        n = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        con.close()
        return (got and got[0] == "ok"), n
    except sqlite3.Error as ex:
        return False, str(ex)


def _restore_snapshot(cfg, target):
    ok, n = _integrity(target)
    if not ok:
        print("这个快照读不了，没动当前库：%s（%s）" % (target, n))
        return 1

    if FLAGS.dry_run:
        cur = sqlite3.connect(str(cfg.db))
        try:
            n_unit = cur.execute("SELECT COUNT(*) FROM unit").fetchone()[0]
        except sqlite3.Error:
            n_unit = "?"
        cur.close()
        print("[dry-run] 会用 %s（%d 张表）覆盖当前库（unit %s 行）"
              % (os.path.basename(target), n, n_unit))
        return 0

    ts = time.strftime("%Y%m%d-%H%M%S")
    keep = "%s.%s.before-undo.bak" % (cfg.db, ts)
    shutil.copy2(cfg.db, keep)
    print("改回之前先把当前库另存：%s" % os.path.basename(keep))

    # 边角文件一起清掉，否则新库带着旧库的预写日志会读到半截状态
    for suffix in ("-wal", "-shm"):
        p = str(cfg.db) + suffix
        if os.path.exists(p):
            os.remove(p)
    shutil.copy2(target, cfg.db)

    ok, n = _integrity(str(cfg.db))
    if not ok:
        print("回滚后自检没过，把刚才另存的那份拷回去再试：%s" % keep)
        return 1
    con = sqlite3.connect(str(cfg.db))
    rows = con.execute("SELECT COUNT(*) FROM unit").fetchone()[0]
    ids = con.execute(
        "SELECT COUNT(DISTINCT bgm_id) FROM unit WHERE bgm_id IS NOT NULL").fetchone()[0]
    con.close()
    print("已回滚到 %s" % os.path.basename(target))
    print("现在 unit %d 行、%d 个不同条目" % (rows, ids))
    return 0


def _restore_bgm(cfg, backup_json):
    if not backup_json.exists():
        print("没有 %s —— 这个工程的 apply / assign 还没跑过。" % backup_json.name)
        return 1
    try:
        data = json.loads(backup_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        print("备份文件读不出来：%s" % ex)
        return 1

    want = {}
    for r in data:
        if "uid" in r:
            want[int(r["uid"])] = r.get("bgm_id")

    con = sqlite3.connect(str(cfg.db))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT uid, unit, bgm_id FROM unit").fetchall()

    changed, missing = [], 0
    for r in rows:
        if r["uid"] not in want:
            missing += 1
            continue
        old = r["bgm_id"] if r["bgm_id"] is not None else None
        new = want[r["uid"]]
        if (old or 0) != (new or 0):
            changed.append((r["unit"], old, new))

    if not changed:
        con.close()
        print("bgm_id 和备份一致，不用改。")
        return 0

    print("要还原 %d 行：" % len(changed))
    for unit, old, new in changed[:15]:
        print("  %s  %s → %s" % ((unit or "")[-52:], old or "-", new or "-"))
    if len(changed) > 15:
        print("  …… 还有 %d 行" % (len(changed) - 15))
    if missing:
        print("备份里没有的 unit %d 个（后来新扫出来的，保持原样）" % missing)

    if FLAGS.dry_run:
        con.close()
        print("\n[dry-run] 不改库。")
        return 0

    ts = time.strftime("%Y%m%d-%H%M%S")
    keep = "%s.%s.before-undo.bak" % (cfg.db, ts)
    shutil.copy2(cfg.db, keep)
    print("\n改之前先把当前库另存：%s" % os.path.basename(keep))

    for r in rows:
        if r["uid"] in want:
            con.execute("UPDATE unit SET bgm_id=? WHERE uid=?",
                        (want[r["uid"]], r["uid"]))
    con.commit()
    con.close()
    print("已还原 %d 行。要重新生成虚拟库就跑 materialize。" % len(changed))
    return 0


def main(args=None):
    cfg = get_cfg()
    backup_json = cfg.w("unit_bgm_backup.json")
    snaps = _snapshots(cfg)

    if getattr(args, "list", False) or not (
            getattr(args, "snapshot", False) or getattr(args, "bgm", False)):
        return _show(snaps, cfg)

    if getattr(args, "bgm", False):
        bks = _bgm_backups(cfg)
        if not bks:
            print("没有 bgm_id 备份 —— 这个工程的 apply / assign 还没跑过。")
            return 1
        idx = getattr(args, "index", None) or 0
        if idx >= len(bks):
            print("只有 %d 份备份，--index 最大 %d。" % (len(bks), len(bks) - 1))
            return 1
        chosen = bks[idx]
        print("用备份 %s（%s）" % (chosen["name"],
                                  chosen["stamp"] or "无时间戳"))
        return _restore_bgm(cfg, Path(chosen["path"]))

    # --snapshot
    want = getattr(args, "target", None)
    if getattr(args, "latest", False) or not want:
        if not snaps:
            print("work 目录下没有整库快照，没得回滚。")
            return 1
        target = snaps[0]["path"]
    else:
        hits = [s for s in snaps if want in s["name"] or s["name"] == want]
        if not hits:
            print("没有名字含「%s」的快照，跑 --list 看看。" % want)
            return 1
        if len(hits) > 1:
            print("「%s」匹配到 %d 个快照，写全一点。" % (want, len(hits)))
            return 1
        target = hits[0]["path"]
    return _restore_snapshot(cfg, target)
