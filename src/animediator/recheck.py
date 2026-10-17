#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核被误判成「特典目录」的主目录。

════════ 为什么需要它 ════════
50 个条目、94 个 unit 的**主目录**被识别层判成「特典目录」，
里面的正片一条都没标出来 → materialize 拿不到正片 → Season 01 空着。
例：`[VCB-Studio] Re Zero ... [Ma10p_1080p]` 里 25 个文件 `[01]..[25]`，
模型 notes 写的是「目录名明确指出这是该作品的特典目录」，纯属幻觉。

════════ 为什么原来的判断会错 ════════
原 prompt 只给了文件名，**没告诉它这部作品该有多少集正片**。
模型看到 `[Ma10p_1080p]` 这种目录名，就自由心证成"特典"。
这里把 **Bangumi 的正片集数**作为事实锚点一起给它 —— 事实归事实，判断归模型。

════════ 规矩 ════════
文件清单**完整原样**给（不切、不筛、不截断），候选/集数全给，代码不替模型下结论。
输出只写 `work/recheck_main.json`，**不动数据库**
（materialize 读它覆盖 kind，可随时回滚）。
"""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .config import get as get_cfg
from .providers import ask_json, require

# sqlite 连接不能跨线程用，所以 prompt 要用的名字先预取进 dict
_n_main = {}
_subj_name = {}


def prompt_for(u, fs, cap):
    nm = _subj_name.get(u["bgm_id"]) or "（未知）"
    lst = "\n".join("%d. %s" % (i, os.path.basename(f["path"] or ""))
                    for i, f in enumerate(fs, 1))
    return """你在核对一个动漫资源目录的归类。下面是这个目录的**完整**文件清单。

目录完整路径：
%s

这个目录被归到的作品：《%s》
该作品在 Bangumi 数据库里的**正片集数：%d 集**

目录下的全部文件（原始文件名，共 %d 个，一个字都没改过）：
%s

──── 请回答 ────
这个目录里，哪些文件是**正片**（TV/网络正式播出的剧集，不是特典、不是菜单、不是 NCOP/NCED、不是 PV/CM）？

注意：
- 正片的判断依据是文件名里的**集号**（如 `[01]` `[02]`，或 `- 01`，或 `第01话`）
- 画质标记 `1080p` `Ma10p` `x265` `FLAC` `BDRip` `HEVC` `10bit` `VFR` 都**不是**集号
- 同一个目录里可能既有正片也有特典，分别标
- 如果这个目录确实一个正片都没有（纯 SPs / 纯 menu / 纯 NCOP），如实说没有

只输出 JSON：
{"reading": "你怎么看这个目录，一句话",
 "has_main": true 或 false,
 "files": {"1": "5", "2": "6", "3": "特CM 宣传片", ...}}

`files` 里每个文件都要有一条，一个都不能漏。值三选一：
  · 纯数字 = 正片第几集
  · 以「特」开头 = 特典，后面写是什么
  · 以「疑」开头 = 拿不准，写原因
""" % (u["unit"], nm, cap, len(fs), lst)


def run(item):
    u, fs, cap = item
    d = ask_json(prompt_for(u, fs, cap))
    if not isinstance(d, dict):
        return str(u["uid"]), {"error": "llm-none"}
    got = d.get("files") or {}
    # 校验：每个文件都得有一条
    miss = [i for i in range(1, len(fs) + 1) if str(i) not in got]
    n_digit = sum(1 for v in got.values()
                  if isinstance(v, str) and v.strip().isdigit())
    n_te = sum(1 for v in got.values()
               if isinstance(v, str) and v.strip().startswith("特"))
    return str(u["uid"]), {"bgm_id": u["bgm_id"], "unit": u["unit"],
                           "cap": cap, "n_files": len(fs),
                           "has_main": bool(d.get("has_main")),
                           "reading": d.get("reading"),
                           "n_digit": n_digit, "n_te": n_te,
                           "missing": miss[:10], "files": got,
                           "paths": [f["path"] for f in fs],
                           "ufile_ids": [f["i"] for f in fs]}


def main(args=None):
    require()                       # 这一步全靠模型判断，没配语义层就别假装跑
    cfg = get_cfg()
    global _n_main, _subj_name

    con = sqlite3.connect(str(cfg.db))
    con.row_factory = sqlite3.Row
    b = sqlite3.connect(str(cfg.bgm_db))

    _n_main = {s: c for s, c in b.execute(
        "SELECT subject_id, COUNT(*) FROM episode WHERE type=0 AND sort>0 "
        "GROUP BY subject_id")}
    _subj_name = {r[0]: (r[1] or r[2] or "") for r in
                  b.execute("SELECT id, name_cn, name FROM subject")}

    units = [dict(r) for r in con.execute("SELECT * FROM unit")]
    files = {}
    for r in con.execute("SELECT * FROM ufile ORDER BY uid, i"):
        files.setdefault(r["uid"], []).append(dict(r))

    # 条目有正片容量、却没有任何 unit 贡献正片 → 这些 unit 全被误判了
    has_main = {}
    for u in units:
        n = sum(1 for f in files.get(u["uid"], []) if f["kind"] == "正片")
        if n:
            has_main[u["bgm_id"]] = has_main.get(u["bgm_id"], 0) + n

    targets = []
    for u in units:
        cap = _n_main.get(u["bgm_id"], 0)
        if cap <= 0:
            continue
        if has_main.get(u["bgm_id"], 0) > 0:
            continue                      # 这部已经有正片了，不折腾
        fs = files.get(u["uid"], [])
        if not fs:
            continue
        targets.append((u, fs, cap))

    print("需要复核的 unit: %d 个" % len(targets))
    if (args is not None and getattr(args, "list", False)) or "--list" in __import__("sys").argv:
        for u, fs, cap in targets:
            print("  uid=%-4s %3d 文件 容量%-3d %s"
                  % (u["uid"], len(fs), cap, (u["unit"] or "")[-64:]))
        return 0

    todo = targets
    limit = getattr(args, "limit", None) if args is not None else None
    if limit:
        todo = targets[:int(limit)]
        print("只处理前 %d 个" % len(todo))

    res = {}
    workers = int(cfg.semantic.get("workers", 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for uid, r in ex.map(run, todo):
            res[uid] = r
            if "error" in r:
                print("  uid=%-4s 失败 %s" % (uid, r["error"]))
            else:
                print("  uid=%-4s %3d 文件  正片 %-3d 特典 %-3d  missing %d  %s"
                      % (uid, r["n_files"], r["n_digit"], r["n_te"], len(r["missing"]),
                         (r["unit"] or "")[-40:]))
            json.dump(res, open(cfg.recheck_json, "w"), ensure_ascii=False, indent=1)

    json.dump(res, open(cfg.recheck_json, "w"), ensure_ascii=False, indent=1)
    ok = [v for v in res.values() if "error" not in v]
    recovered = [v for v in ok if v["n_digit"] > 0]
    print("\n════ 结果 ════")
    print("复核 %d 个 unit：模型认为有正片的 %d 个（原判全是特典）"
          % (len(ok), len(recovered)))
    print("找回正片 %d 条 → %s" % (sum(v["n_digit"] for v in ok), cfg.recheck_json))
    con.close()
    b.close()
    return 0
