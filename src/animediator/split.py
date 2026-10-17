#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多季混目录拆分器 —— 一个 unit 里装了两季时，拆开分别归到各自的 bgm 条目。

════════ 为什么需要它 ════════
`[ANK&VCB-Studio][Code Geass Lelouch of the Rebellion]` 这一个目录里同时有
R1（25 集）和 R2（25 集）。归库层只能给一个 id，materialize 拿到 52 个正片
去对 25 个集号 → `fits()` 失败 → 兜底全给 None → **整批正片降级成特典**。
实测 29 个 unit 中招，76 部番的 Season 01 因此是空的。

════════ 分工（这个项目定下的规矩）════════
代码只做**结构化**：把该 unit 的正片完整文件清单按原顺序编上号，候选条目从
bgm 的 relation 表里取（不筛不截断），原样交给模型。
**判断全交模型**：哪些文件是一组、这组属于哪个条目，代码一句不猜。

════════ 队列必须在"复核之后"的视图上算 ════════
踩过的坑：拆分队列先于复核建好，于是「复核才被救回成正片」的 unit
（识别层把它们整批误判成特典）永远进不了队列 —— 孤独摇滚总集篇、
来自深渊 3 部剧场版、进击最终季都是这么漏的。
"""
import json
import os
import sqlite3
import sys

from .config import FLAGS, get as get_cfg
from .providers import ask_json, require

_con = None
_b = None
_n_main = {}


def candidates(u):
    """当前条目 + relation 表里所有正片数>0 的条目。不筛不截断。"""
    out, seen = [], set()
    row = _b.execute("SELECT id,name,name_cn,date FROM subject WHERE id=?",
                     (u["bgm_id"],)).fetchone()
    if row:
        out.append({"id": row[0], "name": row[1], "name_cn": row[2],
                    "year": (row[3] or "")[:4], "eps": _n_main.get(row[0], 0)})
        seen.add(row[0])
    for r in _b.execute("""
            SELECT s.id, s.name, s.name_cn, s.date
            FROM subject_relation r JOIN subject s ON s.id = r.related_id
            WHERE r.subject_id = ?""", (u["bgm_id"],)).fetchall():
        if r[0] in seen:
            continue
        c = _n_main.get(r[0], 0)
        if c <= 0:          # 没有正片集的（OST/CD/画集）不可能是视频归属
            continue
        out.append({"id": r[0], "name": r[1], "name_cn": r[2],
                    "year": (r[3] or "")[:4], "eps": c})
        seen.add(r[0])
    return out


def build_prompt(u, main, cap, cands):
    lst = []
    for i, f in enumerate(main, 1):
        lst.append("%d. %s" % (i, os.path.basename(f["path"] or "")))
    cl = []
    for c in cands:
        cl.append("- id=%d  %s / %s  (%s)  正片 %d 集"
                  % (c["id"], c["name_cn"] or "", c["name"] or "", c["year"], c["eps"]))
    return """这是一个动漫资源目录，一个目录里可能装了同一系列的多部作品（不同季、剧场版、OVA 等）。

目录名：
%s

这个目录里的正片文件（共 %d 个，按文件名排序）：
%s

可选的目标条目（Bangumi 数据库里的作品）：
%s

任务：把这些文件分组，每组对应上面一个条目。同一部作品的所有集会连续排列。

输出 JSON：
{"groups": [{"files": [文件序号, ...], "bgm_id": 条目id, "reason": "一句话说明"}]}

硬性要求：
- 每个文件必须出现在恰好一个组里，**一个都不能漏**
- bgm_id 只能从上面给出的候选里选
- 确实不属于任何候选的文件，bgm_id 写 0
- 判断依据就是文件名本身（里面有作品名、季标识、集号），不要猜
""" % (u["unit"], len(main), "\n".join(lst), "\n".join(cl))


def validate(main, groups):
    """校验模型输出：序号合法、无重复、无遗漏。返回 (ok, 说明)"""
    seen = []
    for g in groups:
        for i in g.get("files") or []:
            if not isinstance(i, int) or i < 1 or i > len(main):
                return False, "序号越界 %r" % (i,)
            seen.append(i)
    if len(seen) != len(set(seen)):
        return False, "有文件被分进多个组"
    missing = set(range(1, len(main) + 1)) - set(seen)
    if missing:
        return False, "漏了 %d 个文件（如 %s）" % (len(missing), sorted(missing)[:5])
    return True, "ok"


def main(args=None):
    require()
    cfg = get_cfg()
    global _con, _b, _n_main

    redo = bool(getattr(args, "redo", False))
    skip_valid = not redo        # 已存在的结果里 valid=True 的不再重跑（补增量用）

    _con = sqlite3.connect(str(cfg.db))
    _con.row_factory = sqlite3.Row
    _b = sqlite3.connect(str(cfg.bgm_db))

    # 每个条目的正片集数
    _n_main = {}
    for sid, c in _b.execute(
            "SELECT subject_id, COUNT(*) FROM episode WHERE type=0 AND sort>0 "
            "GROUP BY subject_id"):
        _n_main[sid] = c

    units = [dict(r) for r in _con.execute("SELECT * FROM unit")]
    files = {}
    for r in _con.execute("SELECT * FROM ufile ORDER BY uid, i"):
        files.setdefault(r["uid"], []).append(dict(r))

    # ★ 队列要在**复核之后的视图**上算（见模块 docstring）
    _rc = {}
    if cfg.recheck_json.exists():
        try:
            _rc = json.load(open(cfg.recheck_json, encoding="utf-8"))
        except Exception:
            _rc = {}
    for _uid, r in _rc.items():
        if not isinstance(r, dict) or r.get("error") or not r.get("files"):
            continue
        fs = files.get(int(_uid)) or []
        ids = r.get("ufile_ids") or []
        by = {f["i"]: f for f in fs}
        for pos, v in r["files"].items():
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                continue
            if not (1 <= pos <= len(ids)):
                continue
            f = by.get(ids[pos - 1])
            if not f or not isinstance(v, str):
                continue
            v = v.strip()
            if v.isdigit():
                f["kind"], f["ep"] = "正片", int(v)
            elif v.startswith("特"):
                f["kind"], f["ep"] = "特典", None
            elif v.startswith("疑") and r.get("has_main"):
                f["kind"], f["ep"] = "正片", None

    # ── 挑出「正片数 > 条目集数」的 unit ──
    targets = []
    for u in units:
        if not u["bgm_id"]:
            continue
        mn = [f for f in files.get(u["uid"], []) if f["kind"] == "正片"]
        if not mn:
            continue
        cap = _n_main.get(u["bgm_id"], 0)
        if len(mn) > cap:
            targets.append((u, mn, cap))

    print("正片数超出条目容量的 unit: %d" % len(targets))
    if (args is not None and getattr(args, "list", False)) or "--list" in sys.argv:
        for u, m, c in targets:
            print("  uid=%-4s 正片%-4d 容量%-4d %s"
                  % (u["uid"], len(m), c, (u["unit"] or "")[:56]))
        return 0

    result = {}
    # 补增量：把已有结果读回来，valid=True 的不重跑，最终合并写回
    if cfg.split_json.exists():
        try:
            result = json.load(open(cfg.split_json, encoding="utf-8"))
        except Exception:
            result = {}
    _before = len(result)
    if skip_valid:
        targets = [(u, m, c) for (u, m, c) in targets
                   if not (result.get(str(u["uid"])) or {}).get("valid")]
    print("新增待拆 %d 个（已有 %d 个结果）" % (len(targets), _before))
    todo = targets
    limit = getattr(args, "limit", None) if args is not None else None
    if limit:
        todo = targets[:int(limit)]

    for idx, (u, mn, cap) in enumerate(todo, 1):
        cands = candidates(u)
        print("\n[%d/%d] uid=%s 正片%d 容量%d  %s"
              % (idx, len(todo), u["uid"], len(mn), cap, (u["unit"] or "")[:50]))
        print("   候选 %d 个: %s" % (len(cands), ", ".join(str(c["id"]) for c in cands[:8])))
        prompt = build_prompt(u, mn, cap, cands)
        d = ask_json(prompt)
        if not isinstance(d, dict):
            print("   模型无输出，跳过")
            result[str(u["uid"])] = {"error": "llm-none"}
            continue
        groups = d.get("groups") or []
        ok, why = validate(mn, groups)
        print("   模型给 %d 组 → 校验 %s" % (len(groups), "通过" if ok else "失败: " + why))
        for g in groups:
            n = len(g.get("files") or [])
            nm = next((c["name_cn"] or c["name"] for c in cands
                       if c["id"] == g.get("bgm_id")), "?")
            print("      bgm=%-7s %2d 个文件  %s" % (g.get("bgm_id"), n, str(nm)[:30]))
        result[str(u["uid"])] = {"bgm_id": u["bgm_id"], "unit": u["unit"],
                                 "n_main": len(mn), "cap": cap,
                                 "valid": ok, "why": why,
                                 "groups": groups,
                                 "paths": [f["path"] for f in mn],
                                 "ufile_index": [f["i"] for f in mn]}
        json.dump(result, open(cfg.split_json, "w"), ensure_ascii=False, indent=1)

    json.dump(result, open(cfg.split_json, "w"), ensure_ascii=False, indent=1)
    okc = sum(1 for v in result.values() if v.get("valid"))
    print("\n════ 结果 ════")
    print("处理 %d 个 unit，校验通过 %d 个 → %s" % (len(result), okc, cfg.split_json))
    _con.close()
    _b.close()
    return 0
