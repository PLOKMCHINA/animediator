"""人工指定某个目录对应哪部作品。

用不上的时候想不到它，真认错了就离不开：模型把《Charlotte》认成《Queen Charlotte》，
或者一个目录里塞了两季而模型只认出一季——这类错误改数据库很别扭，改虚拟库又会被
下次 materialize 覆盖。这里给一条正规通道。

    animediator lib assign --list                      看所有单元现在的归属
    animediator lib assign --list --unmatched          只看还没认出来的
    animediator lib assign --unit charlotte --search 夏洛特   先搜候选
    animediator lib assign --unit charlotte --bgm 122277      直接指定

写入的是 unit 表，src 记成 manual、conf 记成 high，后面 materialize 会照这份走。
改动前整库快照 + 全表 bgm_id 备份，和 apply 一致。加 --dry-run 只打印不写。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time

from .config import FLAGS, get as get_cfg

# 从离线索引回填到 unit 表的字段（表列名 -> subject 表列名）
FIELDS = [("bgm_id", "id"), ("name_cn", "name_cn"), ("name_ja", "name"),
          ("date", "date"), ("summary", "summary")]


def _snapshot(cfg, con) -> str:
    """改动前留一份能回滚的东西。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    snap = cfg.w("animediator.db.%s.bak" % ts)
    shutil.copy2(cfg.db, snap)
    rows = [dict(r) for r in con.execute(
        "SELECT uid, disk, unit, bgm_id, src, conf FROM unit")]
    bk = cfg.w("unit_bgm_backup.json")
    if not bk.exists():
        bk.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(snap)


def _lookup_subject(bgm_db, bgm_id):
    con = sqlite3.connect(str(bgm_db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, name, name_cn, date, summary FROM subject WHERE id=?",
        (bgm_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def _search(bgm_db, word, limit=15):
    """按中文名或原名搜，只用于给人看候选。"""
    con = sqlite3.connect(str(bgm_db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT DISTINCT s.id, s.name, s.name_cn, s.date FROM sname n "
        "JOIN subject s ON s.id = n.subject_id "
        "WHERE n.txt LIKE ? OR s.name_cn LIKE ? LIMIT ?",
        ("%" + word + "%", "%" + word + "%", limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _list(cfg, unmatched_only=False):
    con = sqlite3.connect(str(cfg.db))
    con.row_factory = sqlite3.Row
    sql = "SELECT uid, unit, bgm_id, name_cn, src, conf FROM unit"
    if unmatched_only:
        sql += " WHERE bgm_id IS NULL OR bgm_id = 0"
    rows = con.execute(sql + " ORDER BY unit").fetchall()
    con.close()
    if not rows:
        print("没有匹配的单元。")
        return 0
    print("%-52s %-8s %-18s %-6s" % ("目录", "bgm id", "中文名", "来源"))
    print("-" * 92)
    for r in rows:
        print("%-52s %-8s %-18s %-6s" % (
            (r["unit"] or "")[:50].rstrip("/"),
            r["bgm_id"] or "-",
            (r["name_cn"] or "")[:16] or "-",
            r["src"] or "-"))
    print("\n共 %d 个单元" % len(rows))
    return 0


def main(args=None):
    cfg = get_cfg()

    # 只给 --search：纯粹帮人找 id，不需要指定 unit
    want = getattr(args, "search", None)
    if want and not getattr(args, "unit", None):
        hits = _search(cfg.bgm_db, want)
        if not hits:
            print("索引里没搜到「%s」。" % want)
            return 1
        print("候选（用 --bgm <id> 选定）：")
        for h in hits:
            print("  %-8s %-28s %s" % (h["id"], (h["name_cn"] or "")[:26],
                                       h["date"] or ""))
        return 0

    if getattr(args, "list", False) or not getattr(args, "unit", None):
        return _list(cfg, unmatched_only=getattr(args, "unmatched", False))

    pattern = getattr(args, "unit", None)
    con = sqlite3.connect(str(cfg.db))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT uid, unit, bgm_id, name_cn FROM unit").fetchall()

    picked = [r for r in rows if pattern.lower() in (r["unit"] or "").lower()]
    if not picked:
        print("没有目录名含「%s」的单元。跑 --list 看看有哪些。" % pattern)
        con.close()
        return 1
    if len(picked) > 1 and not getattr(args, "all", False):
        print("匹配到 %d 个单元，请写得更具体一些，或者加 --all 一次全改：" % len(picked))
        for r in picked[:20]:
            print("  %s" % (r["unit"] or "").rstrip("/"))
        con.close()
        return 1

    # --search：只帮人找 bgm_id，不写库
    want = getattr(args, "search", None)
    if want:
        hits = _search(cfg.bgm_db, want)
        if not hits:
            print("索引里没搜到「%s」。" % want)
        else:
            print("候选（用 --bgm <id> 选定）：")
            for h in hits:
                print("  %-8s %-28s %s" % (h["id"], (h["name_cn"] or "")[:26],
                                           h["date"] or ""))
        con.close()
        return 0

    bgm_id = getattr(args, "bgm", None)
    if not bgm_id:
        print("要给「%s」指定条目，用 --bgm <id>；不确定 id 就先 --search <关键词>。" % pattern)
        con.close()
        return 1

    subject = _lookup_subject(cfg.bgm_db, int(bgm_id))
    if not subject:
        print("离线索引里没有 bgm id %s。先跑 animediator index bgm-build。" % bgm_id)
        con.close()
        return 1

    for r in picked:
        print("%s → %s  %s" % ((r["unit"] or "").rstrip("/")[:46],
                               bgm_id, subject.get("name_cn") or subject.get("name")))

    if FLAGS.dry_run:
        print("\n（dry-run）不写库。")
        con.close()
        return 0

    snap = _snapshot(cfg, con)
    print("\n已备份：%s" % snap)

    for r in picked:
        con.execute(
            "UPDATE unit SET bgm_id=?, name_cn=?, name_ja=?, date=?, summary=?, "
            "src='manual', conf='high' WHERE uid=?",
            (subject["id"], subject.get("name_cn"), subject.get("name"),
             subject.get("date"), subject.get("summary"), r["uid"]))
    con.commit()
    con.close()
    print("已更新 %d 个单元。重跑 animediator lib materialize 让改动生效。" % len(picked))
    return 0
