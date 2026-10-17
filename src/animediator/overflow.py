#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""溢出组重判 —— 拆分后某个组的集号仍装不进条目容量时的最后一环。

════════ 三类溢出（全部实测）════════
A. 识别层把 SP/特典错标成正片，且错在**中间**（ep=[1(SP),2..14]）→ 重复或错位
B. 识别层少读一个集号，后面的全错位（ep=[1..13,None,15..] 或 [1..22,22]）
C. 真正横跨多个条目（银魂. 51 集 = 317-328 + 342-367，跨 3 个 bgm 条目）

════════ 分工 ════════
代码：把该组的**完整**文件清单 + 集号空间（来自 bgm，一个不漏）+ 当前归属交模型。
模型：重新给出「文件 → 集号/特典」和「属于哪个条目」。
代码：只校验（不重不漏、集号在空间内），不猜不改。

════════ ⚠️ prompt 里必须真的把清单塞进去 ════════
初版模板里写了「下面是完整清单」却**忘了 `%s` 占位符**，模型只能凭目录名瞎猜 ——
它老老实实回答「未提供具体文件名，无法判断」，30 个文件全给了特典。
加上清单后同一个 unit 立刻给对：
    `EVA 30 个文件 → 26 集正片 + 4 个（21-24 集的 DC/OnAir 双版本）`
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

from .config import get as get_cfg
from .providers import ask_json, require

_con = None
_b = None
_units = []
_files = {}
_n_main = {}
_subj_name = {}


def sort_space(bid):
    return sorted(int(r[0]) for r in
                  _b.execute("SELECT sort FROM episode WHERE subject_id=? "
                             "AND type=0 AND sort>0", (bid,)).fetchall())


def build_prompt(unit_name, bid, cur_name, ss, fs):
    lst = []
    for i, f in enumerate(fs, 1):
        ep = f["ep"]
        ep_s = str(ep) if ep is not None else "无"
        lst.append("%d. [现标=%s] %s" % (i, ep_s, os.path.basename(f["path"] or "")))
    space = " ".join(str(x) for x in ss) if ss else "（空）"
    return """下面是一个动漫资源目录里的全部正片文件（完整清单，共 %d 个）。

目录名：
%s

目前这套文件被归到作品：《%s》（bgm id=%d）
该作品在 Bangumi 的正片集号空间（共 %d 个）：%s

目录下的全部文件（完整清单，原始文件名，一个字都没改过）：
%s

──── 请判断 ────
1. 这些文件里，哪些是**真正片**（正式剧集），哪些其实是特典/SP/OP/ED/PV/Menu 被误标了？
   文件名里有 `[Sanitarybox]` `[SP]` `[NCOP]` `[Menu]` `[PV]` `[OVA]` `[Picture Drama]`
   `[Live]` `[omake]` 这类字样的，通常不是正片——但最终以你读到的文件名为准。
2. 每个真正片文件的**集号**是多少？（从文件名里的 `[数字]`、`- 数字`、`第N话` 读；
   `1080p`、`Ma10p`、`x265`、`FLAC`、`BDRip`、`HEVC`、`10bit` 都不是集号）
3. 如果这套文件实际属于**不止一部作品**（比如目录名写着 317-367 而上面条目只覆盖一部分），
   请说明应该分成哪些段、各属于哪个作品（可以答「不知道」，但不要编造）。

只输出 JSON：
{"reading": "你怎么看这套文件，一两句",
 "assigns": {"1": "特Sanitarybox 番外", "2": "1", "3": "2", ...},
 "belongs_to": %d}

`assigns` 的键是文件序号（1..%d），每个都要有，一个不能漏。值：
  · 数字 = 正片第几集
  · 以「特」开头 = 特典，后面写它是什么
  · 以「疑」开头 = 拿不准，写原因
""" % (len(fs), unit_name, cur_name, bid, len(ss), space, "\n".join(lst), bid, len(fs))


def run_one(key, u, fs, ss, bid):
    d = ask_json(build_prompt(u["unit"], bid, _subj_name.get(bid, "?"), ss, fs))
    if not isinstance(d, dict):
        return key, {"error": "llm-none"}
    got = d.get("assigns") or {}
    miss = [i for i in range(1, len(fs) + 1) if str(i) not in got]
    return key, {"bgm_id": bid, "uid": u["uid"], "n_files": len(fs),
                 "cap": len(ss), "space": ss,
                 "reading": d.get("reading"),
                 "belongs_to": d.get("belongs_to"),
                 "assigns": got, "missing": miss[:10],
                 "paths": [f["path"] for f in fs],
                 "ufile_ids": [f["i"] for f in fs]}


def _grouped(splits):
    """复现 materialize 的拆分分组：{bgm_id: [unit,...]}"""
    groups = defaultdict(list)
    for u in _units:
        sp = splits.get(str(u["uid"]))
        if sp and sp.get("valid") and len(sp.get("groups") or []) > 1:
            for g in sp["groups"]:
                gid = g.get("bgm_id") or u["bgm_id"]
                if g.get("files"):
                    groups[str(gid)].append(u)
        else:
            groups[str(u["bgm_id"])].append(u)
    return groups


def main(args=None):
    require()
    cfg = get_cfg()
    global _con, _b, _units, _files, _n_main, _subj_name

    _con = sqlite3.connect(str(cfg.db))
    _con.row_factory = sqlite3.Row
    _b = sqlite3.connect(str(cfg.bgm_db))
    _b.row_factory = sqlite3.Row

    _n_main = {s: c for s, c in _b.execute(
        "SELECT subject_id, COUNT(*) FROM episode WHERE type=0 AND sort>0 "
        "GROUP BY subject_id")}
    _subj_name = {r[0]: (r[1] or r[2] or "") for r in
                  _b.execute("SELECT id, name_cn, name FROM subject")}

    _units = [dict(r) for r in _con.execute("SELECT * FROM unit")]
    _files = {}
    for r in _con.execute("SELECT * FROM ufile ORDER BY uid, i"):
        _files.setdefault(r["uid"], []).append(dict(r))

    splits = json.load(open(cfg.split_json, encoding="utf-8"))
    recheck = json.load(open(cfg.recheck_json, encoding="utf-8"))

    # 复现 materialize 的修正后的视图
    # ⚠️ 这里必须和 materialize 的视图**完全一致**，否则"判定溢出"用的状态和
    #    "真正输出"用的状态不是一个东西 —— 会漏判或错判。
    #    ⚠️ 原来这段漏了「疑 + has_main」那条（materialize 和 split 都有），
    #       导致三个模块对"哪些文件是正片"的判断不一致。补上。
    for uid_s, r in recheck.items():
        if not isinstance(r, dict) or r.get("error") or not r.get("files"):
            continue
        fs = _files.get(int(uid_s)) or []
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

    todo = []
    for bid_s, us in _grouped(splits).items():
        bid = int(bid_s)
        ss = sort_space(bid)
        # 该条目下的所有正片（按拆分组聚合）
        agg = []
        for u in us:
            agg += [f for f in _files.get(u["uid"], []) if f["kind"] == "正片"]
        if not agg:
            continue
        # 模拟 assign 的三个偏移，判断是否溢出
        eps = [f["ep"] for f in agg]
        got = [e for e in eps if e is not None]
        ok = False
        if len(got) == len(eps) and len(set(got)) == len(got):
            cc = Counter(ss)
            for off in (0,):
                if all((e + off) in cc for e in got):
                    ok = True
            if not ok and ss:
                lo = min(ss)
                if all((e + (lo - min(got))) in cc for e in got):
                    ok = True
        if ok:
            continue
        # 溢出 → 该组整体（连同它的 unit 集合）交模型
        todo.append((bid, us, agg, ss))

    print("溢出组: %d 个" % len(todo))
    res = {}
    # 补增量：已有结果不重跑，合并写回
    if cfg.overflow_json.exists():
        try:
            res = json.load(open(cfg.overflow_json, encoding="utf-8"))
        except Exception:
            res = {}
    _before = len(res)
    todo = [(b, u, a, s) for (b, u, a, s) in todo
            if not (res.get(str(b)) or {}).get("assigns")]
    print("新增待判 %d 个（已有 %d 个结果）" % (len(todo), _before))
    limit = getattr(args, "limit", None) if args is not None else None
    if limit:
        todo = todo[:int(limit)]
        print("只处理前 %d 个" % len(todo))
    for bid, us, agg, ss in todo:
        key = str(bid)
        print("\n[bgm=%s] 《%s》 正片 %d 个 / 容量 %d"
              % (bid, _subj_name.get(bid, "?"), len(agg), len(ss)))
        print("   units: %s" % [u["uid"] for u in us])
        print("   集号空间: %s" % ("%s..%s" % (ss[0], ss[-1]) if ss else "空"))
        # 银魂式跨条目：文件数 >> 容量 → belongs_to 由模型自由回答，
        # 代码不做候选约束（避免又变成"我替模型筛"）
        k, r = run_one(key, us[0], agg, ss, bid)
        res[k] = r
        if "error" in r:
            print("   失败: %s" % r["error"])
        else:
            n_dig = sum(1 for v in r["assigns"].values()
                        if isinstance(v, str) and v.strip().isdigit())
            print("   reading: %s" % (r.get("reading") or "")[:80])
            print("   正片 %d / 特典 %d / missing %d / belongs_to=%s"
                  % (n_dig, len(r["assigns"]) - n_dig, len(r["missing"]),
                     r.get("belongs_to")))
        json.dump(res, open(cfg.overflow_json, "w"), ensure_ascii=False, indent=1)
    print("\n→ %s" % cfg.overflow_json)
    _con.close()
    _b.close()
    return 0
