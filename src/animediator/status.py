"""看一眼整理流程走到哪一步了，下一步该做什么。

流程有十几步，中间被打断（跑了几小时、隔了几天再来、机器重启过）是常态。
doctor 查的是「环境能不能工作」，这里查的是「这批数据走到哪了」：
每个阶段的产物在不在、有多少，以及下一步该跑什么命令。

只读，不修改任何东西。

    animediator status
"""
from __future__ import annotations

import json
import os
import sqlite3
import unicodedata

from .config import get as get_cfg


def _width(s):
    """终端里的显示宽度：中日韩字符占两格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, n):
    return s + " " * max(0, n - _width(s))

# 每个阶段：怎么判断它做过、做完之后产出什么、给人看的一句话
STAGES = [
    ("离线索引", None, "bgm db 已就绪", "animediator index bgm-build"),
    ("扫描", None, "个目录", "animediator lib scan"),
    ("判定", None, "个目录已判定", "animediator lib identify"),
    ("归库", None, "个单元有归属", "animediator lib resolve"),
    ("出库", None, "条链接", "animediator lib materialize"),
]


def _count_jsonl(p):
    if not p.exists():
        return None
    n = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _db_counts(cfg):
    if not cfg.db.exists():
        return {}
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % cfg.db, uri=True)
        for key, sql in (
                ("unit", "SELECT COUNT(*) FROM unit"),
                ("matched", "SELECT COUNT(*) FROM unit WHERE bgm_id IS NOT NULL"),
                ("files", "SELECT COUNT(*) FROM ufile"),
        ):
            try:
                out[key] = con.execute(sql).fetchone()[0]
            except sqlite3.Error:
                pass
        con.close()
    except sqlite3.Error:
        pass
    return out


def _vfs_links(cfg):
    if not cfg.vfs.exists():
        return None
    n = 0
    for _dp, dirnames, filenames in os.walk(cfg.vfs):
        for x in dirnames + filenames:
            if os.path.islink(os.path.join(_dp, x)):
                n += 1
    return n


def main(args=None):
    cfg = get_cfg()

    idx_ok = cfg.bgm_db.exists()
    tree_n = None
    if cfg.tree_json.exists():
        try:
            t = json.loads(cfg.tree_json.read_text(encoding="utf-8"))
            tree_n = sum(len(v.get("dirs") or {}) for v in t.values())
        except (OSError, ValueError):
            tree_n = None
    units_n = None
    if cfg.units_json.exists():
        try:
            units_n = len(json.loads(cfg.units_json.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            units_n = None
    ident_n = _count_jsonl(cfg.brain_jsonl)
    if ident_n is None:
        # 判定结果的文件名随版本改过，退一步找 work 下的识别产物
        for cand in ("identify_v1.jsonl", "units_identified.jsonl"):
            ident_n = _count_jsonl(cfg.w(cand))
            if ident_n is not None:
                break
    dbc = _db_counts(cfg)
    vfs_n = _vfs_links(cfg)

    rows = [
        ("离线索引", None if not idx_ok else "就绪", None,
         cfg.bgm_db.name if idx_ok else "缺 %s" % cfg.bgm_db.name,
         "animediator index bgm-build"),
        ("扫描", tree_n, "个目录", cfg.tree_json.name if tree_n else "没有 tree.json",
         "animediator lib scan"),
        ("判定", ident_n, "个单元", cfg.brain_jsonl.name if ident_n else "没有判定结果",
         "animediator lib identify"),
        ("归库", dbc.get("matched"), "个单元有归属",
         ("共 %d 个单元" % dbc["unit"]) if dbc.get("unit") else "作业库还没建",
         "animediator lib resolve"),
        ("出库", vfs_n, "条链接", cfg.vfs.name if vfs_n else "虚拟库还没生成",
         "animediator lib materialize"),
    ]

    print("工程：%s" % (cfg.path or "(未指定配置文件)"))
    print()
    print("%s%s%s" % (_pad("阶段", 12), _pad("进度", 26), "产物 / 状态"))
    print("─" * 62)
    first_missing = None
    for name, n, unit, note, cmd in rows:
        done = bool(n)
        mark = "✓" if done else "·"
        if n is None or n == 0:
            bar = "未开始"
            if first_missing is None:
                first_missing = (name, cmd)
        elif isinstance(n, str):
            bar = n
        else:
            bar = "%d %s" % (n, unit)
        print("%s %s%s%s" % (mark, _pad(name, 11), _pad(bar, 26), note))

    print()
    if first_missing:
        name, cmd = first_missing
        print("下一步：「%s」还没做，跑" % name)
        print("    %s" % cmd)
    else:
        print("五个阶段都有产物了。")
        print("要接入媒体服务器：animediator serve deploy")
        print("要确认链接没坏：  animediator lib verify")

    # 归库阶段有单元但没归属，通常是语义层不可用或还没跑完
    if dbc.get("unit") and dbc.get("matched") is not None:
        miss = dbc["unit"] - dbc["matched"]
        if miss > 0:
            print()
            print("有 %d 个单元还没有归属（共 %d 个）。" % (miss, dbc["unit"]))
            print("  先确认语义层可用：animediator setup doctor")
            print("  只想手工指定：    animediator lib assign --list --unmatched")
    return 0
