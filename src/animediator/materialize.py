"""输出层 —— 每个 bgm 条目 = 一条扁平的章节表，所以只出 Season 01 + Season 00。

════════ 为什么"季"这一层不能听模型的 ════════
上一版直接用模型给每个 unit 打的 `season` 号。可这是**概念错配**：
    · bgm 的数据模型是**扁平**的 —— 一个 subject 就是一条 sort 1..N 的章节表，
      没有"季"这一层（不同季是**不同 subject**）
    · 而模型看到 `TV S2 Naruto Shippuuden` 就打了 season=2，于是疾风传
      被切成 Season 01/02/03 —— 而它其实只有一条 221-720 的章节表
实测：疾风传出过 Season 01(8) / 02(224) / 03(102) 三个目录，全错。

正确形态：
    <番名> (年份)/
        bangumi.ini          ID=<bgm id>
        Season 01/           ← 正片，集号 = bgm 的 sort
        Season 00/           ← 特典

════════ 集号怎么定（= bgm 的 sort）════════
源头文件名里的集号**不一致**（实测同一个条目里，有的 unit 是局部号 001-032、
有的已经是全局号 253-276）。优先级：
    ① 目录名并排写了局部区间和全局区间 → 偏移量直接推导
       `001-032(221-252)` → 偏移 = 221−1 = 220（**发布者自己写下的对应关系**）
    ② 模型读出的集号，若整段落在这个条目的 sort 区间里，直接用
    ③ 否则整体平移到区间内（偏移量由"落点必须在区间内"这个**事实约束**定）
    ④ 都不行 → 按顺序补齐（只在"模型压根没读出集号"时用，有界猜测）
最终全部对照 bgm 的 sort 集合去重、校验。落不上的**诚实降级**成特典，
不硬塞一个编号进去。

⚠️ **原始文件一个字节不动**，只建软链。软链目标写**容器内路径** ——
   因为要看这些链接的是容器里的 Jellyfin，容器看不到宿主路径。
   单机直跑（不经 Docker）时把 `media.*.container` 填成和 `root` 一样即可。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict

from .config import FLAGS, get as get_cfg

RANGE = re.compile(r"(?<![\d])(\d{2,4})\s*[-~]\s*(\d{2,4})(?![\d])")
PAREN = re.compile(r"\((\d{2,4})\s*[-~]\s*(\d{2,4})\)")

MARK = ".animediator-vfs"


def safe(s):
    """目录名/文件名里不能出现的字符换成空格 —— Jellyfin 在 Windows 上也会读这些名。"""
    s = re.sub(r'[/\\:*?"<>|\r\n\t]', " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:110] or "未命名"


def write_ini(path, bgm_id, extra=None):
    """写 bangumi.ini。字段名照 LocalConfiguration 的属性名（读取时忽略大小写），
    只有 Id 存成 `ID`（见 LocalConfiguration.SaveTo 里的特例）。"""
    lines = ["[Bangumi]", "ID=%d" % int(bgm_id)]
    for k, v in (extra or {}).items():
        lines.append("%s=%s" % (k, v))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def unit_range(u):
    """目录名标注的集号区间。优先括号里的（发布者标明的全局号）。
    参数可以是 unit 字典，也可以是路径字符串。"""
    val = u["unit"] if isinstance(u, dict) else u
    base = os.path.basename(str(val).rstrip("/"))
    m = PAREN.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = RANGE.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def dual_offset(unit):
    """目录名形如 `113-143(333-363)` → 返回 333-113=220。读不到返回 0。
    这是发布者并排写下的局部↔全局对应，纯取值，不是推断。"""
    base = os.path.basename(str(unit).rstrip("/"))
    m = re.search(r"(\d{2,4})\s*[-~]\s*(\d{2,4})\s*[（(]\s*(\d{2,4})\s*[-~]\s*(\d{2,4})", base)
    if m:
        return int(m.group(3)) - int(m.group(1))
    return 0


def assign(unit_files, sorts):
    """给一个 unit 的文件定全局集号。返回 [(file, ep or None)]。

    ⚠️ 关键修正：发布者常把**局部区间和全局区间并排写在目录名里**，
       形如 `113-143(333-363)` / `001-032(221-252)`。
       那么偏移量 = 全局起点 − 局部起点（333−113=220 / 221−1=220）。
       ⚠️ 之前漏了这条，导致 `113-143(333-363)` 被算成 221-251。
    """
    if not unit_files:
        return []
    lo_all = min(sorts) if sorts else None

    n = len(unit_files)
    eps = [f["ep"] for f in unit_files]
    unit = unit_files[0]["_unit"]

    # ① 目录名里并排写了局部区间和全局区间 → 偏移量直接推导
    offs = []
    o = dual_offset(unit)
    if o:
        offs.append(o)
    # ② 模型读的集号原样
    offs.append(0)
    # ③ 整体平移到该条目的起点（当模型读的是全程相对号时）
    if lo_all is not None and any(e is not None for e in eps):
        offs.append(lo_all - min(e for e in eps if e is not None))

    if not sorts:
        return list(zip(unit_files, eps))

    def place(off):
        """按该偏移落点。**逐文件判**：落进集号空间就落，落不上或撞号的给 None。
        撞号时**先到的赢** —— 同一个集号不可能装两个文件，
        与其整批放弃，不如让先到的占位、后来的诚实降级。"""
        taken, out, cnt = set(), [], 0
        for e in eps:
            if e is None:
                out.append(None)
                continue
            c = e + off
            if c in sorts and c not in taken:
                taken.add(c)
                out.append(c)
                cnt += 1
            else:
                out.append(None)
        return cnt, out

    # ⚠️ 原来是「全部落点都在空间内才采用，否则整批 None」——
    #    一个重复集号（识别层把 SP 错标成正片）就能把 200 多集整批判死。
    #    实测 76 部番的 Season 01 因此全空。改成比落点数取最优，冲突的只牺牲自己。
    best_cnt, best_out = 0, None
    for off in offs:
        cnt, out = place(off)
        if cnt > best_cnt:
            best_cnt, best_out = cnt, out

    if best_out is not None and best_cnt > 0:
        return list(zip(unit_files, best_out))

    # ④ 兜底。分两种情况，**不编集号**：
    #    · 模型压根没读出集号（全 None）→ 从该条目起点顺序铺（有界猜测，仅在此时用）
    #    · 模型读出了集号但对不上这个条目 → 一律 None
    #      （典型：一个 unit 横跨两个条目，如 `[银魂.][第四季][317-367TV全集]`
    #        而 bgm 的《银魂.》只到 328 —— 越界的那批不该硬塞进这个条目）
    avail = sorted(sorts) if sorts else []
    if not any(e is not None for e in eps):
        out = [avail[i] if i < len(avail) else None for i in range(n)]
    else:
        out = [None] * n
    return list(zip(unit_files, out))


def check_target(vfs, vrf_force=False):
    """★ 安全闸：确认目标目录可以写。

    这套工具会往目标目录里建几百个目录和几千个软链。指错一个路径
    （比如把 vfs 配成了媒体根目录）就会在别人的收藏里撒一地垃圾。
    所以：目标目录非空、又**看不出是我们生成的库**（没有任何 bangumi.ini），
    就停下来要求显式确认。空目录（首次生成）直接放行。
    """
    if vrf_force or not vfs.exists():
        return
    entries = list(vfs.iterdir())
    if not entries:
        return
    if (vfs / MARK).exists():
        return
    for d in entries:
        if d.is_dir():
            if (d / "bangumi.ini").exists():
                return
            for sub in d.iterdir() if d.is_dir() else []:
                if sub.is_dir() and (sub / "bangumi.ini").exists():
                    return
    raise SystemExit(
        "目标目录不是空的，又看不出是 AniMediator 生成的虚拟库：\n  %s\n\n"
        "虚拟库只放**符号链接**，但为了不往别人的收藏里撒垃圾，这里停下来。\n"
        "  换一个空目录（改配置里的 vfs），或者确认要写这个目录时加 --force。\n"
        "  （原文件不会被碰 —— 这一点有保证；怕的是往错的地方建几千个目录）" % vfs)


def main(args=None):
    cfg = get_cfg()
    force = bool(getattr(args, "force", False))
    out = getattr(args, "out", None)
    vfs = type(cfg.vfs)(out) if out else cfg.vfs
    dry = FLAGS.dry_run

    con = sqlite3.connect(str(cfg.db))
    con.row_factory = sqlite3.Row
    b = sqlite3.connect(str(cfg.bgm_db))

    units = [dict(r) for r in con.execute("SELECT * FROM unit")]
    files = defaultdict(list)
    for f in con.execute("SELECT * FROM ufile ORDER BY uid, i"):
        files[f["uid"]].append(dict(f))

    # bgm 各条目的 sort 集合
    sorts = defaultdict(set)
    for sid, s in b.execute("SELECT subject_id, sort FROM episode WHERE sort>0"):
        sorts[sid].add(int(s))

    # ★ 复核修正：识别层把「主目录」误判成「特典目录」时（50 个条目、94 个 unit 中招，
    #   典型如 `[VCB-Studio] Re Zero ... [Ma10p_1080p]` 里 25 个 `[01]..[25]` 被判成特典），
    #   用 recheck_main.json 的结论覆盖 kind/ep。这份结论是让模型带着「该作品正片集数」
    #   这个事实锚点重新判的，不是我们拿规则替它改。
    recheck = _load_json(cfg.recheck_json)
    n_fix = 0
    for uid_s, r in recheck.items():
        if not isinstance(r, dict) or r.get("error") or not r.get("files"):
            continue
        fs = files.get(int(uid_s)) or []
        if not fs:
            continue
        ids = r.get("ufile_ids") or []
        by_i = {f["i"]: f for f in fs}
        fixed = 0
        for pos, v in r["files"].items():
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                continue
            if not (1 <= pos <= len(ids)):
                continue
            f = by_i.get(ids[pos - 1])
            if not f or not isinstance(v, str):
                continue
            v = v.strip()
            if v.isdigit():
                f["kind"] = "正片"
                f["ep"] = int(v)
                fixed += 1
            elif v.startswith("特"):
                f["kind"] = "特典"
                f["ep"] = None
                f["what"] = v[1:].strip() or "特典"
            elif v.startswith("疑") and r.get("has_main"):
                # 模型**明确表态「这个目录里有正片」**，只是不敢敲定集号。
                # 那就按它的表态落成正片、集号留空 —— 交给 assign() 兜底④
                # （「模型没读出集号」时从该条目起点顺序铺，本来就有的规则）。
                # 不能因为它写了「疑」就把它的表态整个丢掉：实测冰结之绊、
                # 小圆概念视频就是这么丢的（都是只有 1 集的剧场版/概念片）。
                f["kind"] = "正片"
                f["ep"] = None
                fixed += 1
        if fixed:
            n_fix += 1
    if n_fix:
        print("复核修正：%d 个 unit 按模型复判结果改了正片/特典" % n_fix)

    # ★ 溢出组重判：拆分组/整单元的集号仍装不进条目容量时，
    #   让模型带着**完整文件清单 + bgm 集号空间**重判一次。
    #   ⚠️ 必须按**路径**映射、且只作用于「本就属于这个条目」的文件：
    #      同一个 unit 的 52 个文件会被 bid=8 和 bid=793 各判一次，
    #      793 那边把 R2 的 25 个标成「特典（属于另一作品）」——
    #      不做归属隔离就会把 R2 的正片冲掉。
    ovf = _load_json(cfg.overflow_json)
    ovf_by_bid = {}
    for _b, r in ovf.items():
        if not isinstance(r, dict) or r.get("error"):
            continue
        assigns = r.get("assigns") or {}
        paths = r.get("paths") or []
        m = {}
        for pos, v in assigns.items():
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                continue
            if not (1 <= pos <= len(paths)):
                continue
            m[paths[pos - 1]] = v
        if m:
            try:
                ovf_by_bid[int(_b)] = m
            except (TypeError, ValueError):
                continue

    def apply_ovf(bid, fs):
        """把该条目的溢出重判结果作用到文件上（按路径匹配）。
        「疑」开头的值 = 模型拿不准 → **保持原判不动**，不替它下结论。"""
        m = ovf_by_bid.get(bid)
        if not m:
            return 0
        k = 0
        for f in fs:
            v = m.get(f["path"])
            if not isinstance(v, str):
                continue
            v = v.strip()
            if v.isdigit():
                f["kind"], f["ep"] = "正片", int(v)
                k += 1
            elif v.startswith("特"):
                f["kind"], f["ep"] = "特典", None
                f["what"] = v[1:].strip() or "特典"
        return k

    n_ovf = 0
    groups = defaultdict(list)
    # ★ 多季混目录拆分：一个 unit 里装了两季时，按 split 的结论拆成虚拟 unit。
    #   不拆的话，52 个正片去对 25 个集号，fits() 失败 → 兜底全给 None
    #   → **整批正片降级成特典**，Season 01 空着（实测 33 个 unit 中招、76 部番受害）。
    splits = _load_json(cfg.split_json)
    n_split = 0
    for u in units:
        fs = files.get(u["uid"], [])
        sp = splits.get(str(u["uid"]))
        if sp and sp.get("valid") and len(sp.get("groups") or []) > 1:
            main = [f for f in fs if f["kind"] == "正片"]
            taken = set()
            for gi, g in enumerate(sp["groups"]):
                gid = g.get("bgm_id") or u["bgm_id"]   # 模型说不属于任何候选时回退原条目，不丢文件
                sel = [main[k - 1] for k in (g.get("files") or [])
                       if isinstance(k, int) and 1 <= k <= len(main)]
                if not sel:
                    continue
                taken.update(id(f) for f in sel)
                nu = dict(u)
                nu["bgm_id"] = gid
                nu["_split"] = "%d/%d" % (gi + 1, len(sp["groups"]))
                extra = [] if gi else [f for f in fs if f["kind"] != "正片"]
                scope = sel + extra
                # ★ 溢出重判按路径作用，只碰「本组自己的文件」——
                #   同一 unit 被 8/793 各判一次时不会互相冲掉
                if apply_ovf(gid, scope):
                    n_ovf += 1
                groups[gid].append((nu, scope))
            rest = [f for f in fs if f["kind"] == "正片" and id(f) not in taken]
            if rest:
                nu = dict(u)
                nu["_split"] = "rest"
                apply_ovf(u["bgm_id"], rest)
                groups[u["bgm_id"]].append((nu, rest))
            n_split += 1
        else:
            apply_ovf(u["bgm_id"], fs)
            groups[u["bgm_id"]].append((u, fs))
    if n_ovf:
        print("溢出重判：%d 个组按模型的集号空间复核结果重排" % n_ovf)
    if n_split:
        print("多季混目录拆分：%d 个 unit 按模型结论拆开" % n_split)

    print("单元 %d → %d 个 bgm 条目" % (len(units), len(groups)))
    if not dry:
        check_target(vfs, force)
        vfs.mkdir(parents=True, exist_ok=True)
        (vfs / MARK).write_text(
            "AniMediator 生成的虚拟库。里面只有符号链接和 .ini/.nfo，"
            "随时可以整个删掉重生成。\n", encoding="utf-8")

    # 盘位 → 容器内路径（软链目标必须写成容器视角，见模块 docstring）
    media = cfg.media

    def container_of(disk_key):
        m = media.get(disk_key) or {}
        return (m.get("container") if isinstance(m, dict) else None) or m or ""

    used, made = {}, []
    n_ep = n_ex = n_unc = n_noid = 0
    n_shift = n_ann = 0

    for bid, us in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if not bid:
            n_noid += 1
            continue
        us.sort(key=lambda x: (unit_range(x[0]) or (9999, 9999), x[0]["unit"]))
        head = us[0][0]
        # ★ 目录名用 **bgm 的权威中文名**，不用 unit 表里的 name_cn。
        #   unit 的名字是识别层/归库时写的，可能和 bgm 条目的中文名不一致
        #   （实测：R2 的 unit 名字写成「Code Geass 反叛的鲁路修」，跟 R1 撞名，
        #   于是生成了 `... [2]` 目录，而 Jellyfin 对这种重名目录拿不到 bangumi id）。
        _cr = b.execute("SELECT name_cn, name, date FROM subject WHERE id=?",
                        (bid,)).fetchone()
        name_bgm = ((_cr[0] or _cr[1]) if _cr else None)
        cn = safe(name_bgm or head["name_cn"] or head["name_ja"] or head["unit"])
        yrs = [int(u["year"]) for u, _ in us if str(u["year"] or "").isdigit()]
        yr_bgm = None
        if _cr and _cr[2] and str(_cr[2])[:4].isdigit():
            yr_bgm = int(str(_cr[2])[:4])
        dirname = "%s%s" % (cn, " (%s)" % (yr_bgm if yr_bgm else min(yrs))
                            if (yr_bgm or yrs) else "")
        if dirname in used:
            used[dirname] += 1
            dirname = "%s [%d]" % (dirname, used[dirname])
        else:
            used[dirname] = 1
        base = os.path.join(str(vfs), dirname)
        ss = sorts.get(bid) or set()

        plan, seen_ep, ex_no = [], {}, 0
        for u, fs in us:
            main_f = [f for f in fs if f["kind"] == "正片"]
            oth_f = [f for f in fs if f["kind"] != "正片"]
            main_f.sort(key=lambda f: (f["ep"] if f["ep"] is not None else 9999,
                                       f["path"]))
            for f in main_f:
                f["_unit"] = u["unit"]
                f["_disk"] = u["disk"]
            for f, ep in assign(main_f, ss):
                if ep is not None and ep not in seen_ep:
                    seen_ep[ep] = True
                    plan.append((1, int(ep), "正片", f))
                else:
                    ex_no += 1
                    plan.append((0, ex_no, "特典", f))
            for f in oth_f:
                f["_disk"] = u["disk"]
                ex_no += 1
                plan.append((0, ex_no, "特典", f))

        # 结算标注类型（只为统计）
        for u, fs2 in us:
            mf = [f for f in fs2 if f["kind"] == "正片"]
            if not mf:
                continue
            r = unit_range(u["unit"])
            if r and (r[1] - r[0] + 1) == len(mf):
                n_ann += 1
            else:
                n_shift += 1

        if not plan:
            continue
        if not dry:
            os.makedirs(base, exist_ok=True)
            write_ini(os.path.join(base, "bangumi.ini"), bid)
        made.append(dirname)

        for sn, en, kind, f in plan:
            src = os.path.join(container_of(f.get("_disk") or head["disk"]), f["path"])
            ext = os.path.splitext(f["path"])[1] or ".mkv"
            if sn == 0 and kind == "特典":
                tdir = os.path.join(base, "Season 00")
                fname = "%s - S00E%02d%s" % (cn, en, ext)
            elif sn == 0:
                tdir = os.path.join(base, "待确认")
                fname = "%s - 待确认%03d%s" % (cn, en, ext)
            else:
                tdir = os.path.join(base, "Season 01")
                fname = "%s - S01E%02d%s" % (cn, en, ext)
            if kind == "正片":
                n_ep += 1
            elif kind == "特典":
                n_ex += 1
            else:
                n_unc += 1
            if dry:
                continue
            os.makedirs(tdir, exist_ok=True)
            # ★ Season 目录里也必须有一份 bangumi.ini
            #   EpisodeProvider.GetMetadata → LocalConfiguration.ForPath(info.Path)
            #   ForPath(文件) 读的是**该文件所在目录**的 ini。只写在番目录的话，
            #   集级别拿不到 ID，插件既匹配不上（实测 200 集 0 个 ProviderIds），
            #   中文集名也就落不下来。
            write_ini(os.path.join(tdir, "bangumi.ini"), bid,
                      {"Type": "Special"} if tdir.endswith("Season 00") else None)
            link = os.path.join(tdir, fname)
            if os.path.islink(link):
                os.remove(link)
            elif os.path.exists(link):
                # 真文件挡路 —— 绝不动它，跳过（虚拟库里本来不该有真文件）
                print("   [跳过] 目标已存在且不是软链：%s" % link)
                continue
            os.symlink(src, link)

    print("生成 %d 部 / 正片 %d / 特典 %d / 待确认 %d%s" % (
        len(made), n_ep, n_ex, n_unc, "（dry-run）" if dry else ""))
    print("其中 %d 个 unit 用了目录名标注的集号区间，%d 个靠模型集号/平移"
          % (n_ann, n_shift))
    if not dry:
        print("虚拟库：%s" % vfs)
        print("  ⚠️ 里面只有符号链接和 .ini/.nfo。原始文件一个字节没动。")
    con.close()
    b.close()
    return 0


def _load_json(path):
    if not path.exists():
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
