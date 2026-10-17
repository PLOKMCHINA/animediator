"""归库层 —— 有序校验，不是打分匹配；三段式，网络并行。

════════ 为什么是"有序校验"而不是"打分匹配" ════════
上一版干了两件被点名的事：
  ① 把方括号**删掉**再查（`[VCB-Studio]`、`[Sousou no Frieren]` 一起被删）。
     可番名本身就在方括号里 —— 删完只剩噪声。
     ▶ 名字**不切不删**，由模型读出来；代码原样拿去查。
  ② 按"可信度 × 精确度 × 集数吻合"加权打分。权重是我拍的，
     错例（Frieren→悠悠式）还显示 1.00 high。
     ▶ 改成**有序校验**：按可信顺序试候选名，命中即停。
       没有分数、没有权重、没有模糊 LIKE —— 只有"库里有没有这条名字"。

════════ 三段式（速度） ════════
早先在一版里把「离线查库 + 联网取元数据 + 联网搜番 + 叫模型挑重名」串着跑，
实测 1 分钟 1 个单元，455 个要 7 个多小时 —— 瓶颈是 447 次串行网络请求。
拆开之后：
  ① 离线段：纯 SQLite，毫秒级（**大部分单元在这里就定案了**）
  ② 元数据段：把要取的 bgm_id 去重后**并行**取，取回写进 bgm_cache
  ③ 兜底段：只有本地全不中的少数才联网搜；重名交给模型挑（并行）

**命中来源就是置信度**，不叫分数：
  listing = 模型在**原始文件名里读到的**名字命中了库 → 最硬
  recall  = 模型凭知识想起的名字命中了库 → 次之
  net     = 联网拿到的 → 再次
  none    = 都没中（会走 ③b 让模型翻译中文名）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .bgm import meta
from .config import get as get_cfg
from .db import SCHEMA as WORK_SCHEMA
from .providers import ask_json, require
from .textutil import norm_query as norm     # ⚠️ 查询侧归一化，见 textutil 的说明


def _ensure_name_index(con, cfg) -> None:
    """确保 `name` 表里有东西；空的话用离线 bgm 索引兜底重建。

    ════════ 为什么要有这一步 ════════
    归库的离线段查的是 `name`（norm → aid → anime.bangumi）。这张表原来只从
    外部 aodb 建 —— 也就是说新用户除了 dump.zip 还得多搞两份文件，否则
    **整个离线段一条都命中不了**，455 个单元全推给模型。

    但 1.3 G 的 bgm.db 里 `sname` 本来就有 97 万条别名，够用。
    实测真实 455 个单元的单值命中率：aodb 58.9% / bgm.db 52.5%，
    两边都命中且同一条目的占 43.3%，**分歧只有 2 个**。

    → 所以 aodb 变成「可选加速」，而不是「必须」。只下一个 dump.zip 也能跑通。
    """
    try:
        n = con.execute("SELECT COUNT(*) FROM name").fetchone()[0]
    except Exception:
        n = 0
    if n > 0:
        return
    if not cfg.bgm_db.exists():
        print("⚠️ name 表是空的，bgm.db 也还没建 —— 离线段会完全命中不了。")
        print("   先跑 `animediator bgm-build`（或用 bgm-aodb 建 aodb 名字索引）。")
        return
    from .bgm.aodb import build_from_bgm
    print("name 表为空 → 用离线 bgm 索引兜底重建（不用 aodb 也能继续）")
    build_from_bgm(con, cfg.bgm_db)


def candidates(rec):
    """按可信顺序返回 [(名字, 来源)] —— 不切名字、不打分。"""
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


def main(args=None):
    require()
    cfg = get_cfg()
    src = getattr(args, "input", None) or str(cfg.brain_jsonl)
    rows = [json.loads(l) for l in open(src, encoding="utf-8")]
    con = sqlite3.connect(str(cfg.db), check_same_thread=False)
    # ⚠️ 这一步会**重建** unit / ufile 两张表 —— 归库是整个流程里唯一
    #    有权重写这两张表的地方，后面的复核/拆分/溢出都只在它上面打补丁。
    con.executescript("DROP TABLE IF EXISTS unit; DROP TABLE IF EXISTS ufile;")
    con.executescript(WORK_SCHEMA)
    _ensure_name_index(con, cfg)
    print("识别结果 %d 个单元（来自 %s）" % (len(rows), src), flush=True)

    # ═══ ① 离线段：纯 SQLite，毫秒级 ═══
    plan = []            # 每单元定案结果
    need_meta = set()    # 要取元数据的 bgm_id
    need_net = []        # 本地全不中的单元下标
    amb = []             # 同名多候选的单元下标
    for idx, rec in enumerate(rows):
        best, chosen = None, None
        for name, srckind in candidates(rec):
            aids = [r[0] for r in con.execute(
                "SELECT DISTINCT aid FROM name WHERE norm=?", (norm(name),))]
            if not aids:
                continue
            if len(aids) == 1:
                r = con.execute("SELECT title,bangumi,zh_name,year FROM anime WHERE aid=?",
                                (aids[0],)).fetchone()
                if r:
                    best = {"aid": aids[0], "bgm": r[1], "zh": r[2], "title": r[0],
                            "year": r[3], "matched": name, "src": srckind}
                    break
            else:
                chosen = (name, srckind, aids)
                break
        if chosen:
            amb.append((idx, chosen))
        elif best is None:
            need_net.append(idx)
        plan.append(best)
        if best and best["bgm"]:
            need_meta.add(best["bgm"])
    print("① 离线确定案 %d 单元；多候选待模型挑 %d；本地全不中 %d；涉及 bgm_id %d 个"
          % (sum(1 for b in plan if b), len(amb), len(need_net), len(need_meta)),
          flush=True)

    # ═══ ② 元数据段：并行取，回写离线缓存 ═══
    meta_map = {}
    LOCK = threading.Lock()

    def fetch(bid):
        r = con.execute("SELECT name_cn,name_ja,date,summary,poster FROM bgm_cache "
                        "WHERE bgm_id=?", (bid,)).fetchone()
        if r:
            sub = {"name_cn": r[0], "name": r[1], "date": r[2], "summary": r[3],
                   "images": {"large": r[4]}}
        else:
            sub = meta.subject(bid)
            if sub:
                with LOCK:
                    con.execute("INSERT OR REPLACE INTO bgm_cache VALUES (?,?,?,?,?,?)",
                                (bid, sub.get("name_cn"), sub.get("name"),
                                 sub.get("date"), sub.get("summary"),
                                 (sub.get("images") or {}).get("large")))
                    con.commit()
        with LOCK:
            meta_map[bid] = sub

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        for _ in as_completed([ex.submit(fetch, b) for b in need_meta]):
            pass
    print("② 元数据取回 %d 个（命中缓存/新取），用时 %.0f 秒"
          % (len(meta_map), time.time() - t0), flush=True)

    # ═══ ③ 兜底段：联网搜 + 模型挑重名（并行） ═══
    def do_net(idx):
        """联网兜底 —— 两条纪律，都是踩坑换来的。

        ① **优先用模型的知识名（`recall`）去搜，罗马音排后面**。
           番组计划的搜索对罗马音几乎不可用（`Ansatsu Kyoushitsu` 只返回
           一个错条目、`DanMachi` 会搜到国产动画《丹道宗师》），
           而日文原名搜得很准。`recall` 就是模型给的日文原名。
        ② **搜到之后要让模型自己确认**。搜索结果是**候选**，不是结论 ——
           `DanMachi` 搜到《丹道宗师》就是这么错的。把候选给模型看，
           它说不是就作废。判断归模型，代码只负责把候选端上来。
        """
        rec = rows[idx]
        cands = candidates(rec)
        # 知识名（recall）排前面：日文原名搜得准，罗马音搜不准
        cands.sort(key=lambda x: 0 if x[1] == "recall" else 1)
        for name, srckind in cands:
            for c in meta.search(name)[:3]:
                if not c.get("id"):
                    continue
                sub = meta.subject(c["id"])
                nm = (sub or {}).get("name") or c.get("name") or ""
                cn = (sub or {}).get("name_cn") or c.get("name_cn") or ""
                if not nm and not cn:
                    continue
                # 让模型确认这个番组计划条目是不是它说的那部作品
                # ⚠️ 只要一个数字。之前让它写 `{"same":..., "why":"..."}`，
                #    why 一写长就撞 max_tokens 被截断 → JSON 不完整 → 解析失败，
                #    白白重试。判断本身不需要长篇理由，别给它机会写。
                obj = ask_json(
                    "你之前整理硬盘上这个动漫目录，判断它是一部作品。\n\n"
                    "目录名：%s\n"
                    "你判断的作品：%s（日文原名：%s，年份：%s）\n"
                    "目录里的文件名示例：\n%s\n\n"
                    "现在在番组计划里搜到一个条目，判断**是不是同一部作品**：\n"
                    "  中文名：%s\n  原名：%s\n\n"
                    '只输出 JSON：{"same": 1 或 0}（1=同一部，0=不是）' % (
                        rec["unit"][-80:], rec["result"].get("title") or "?",
                        rec["result"].get("recall") or "无",
                        rec["result"].get("year") or "未知",
                        "\n".join("   " + f[-70:] for f in rec["files"][:8]),
                        cn or "无", nm or "无"),
                    # ⚠️ 长 prompt 下推理模型会强行思考，额度给小了 content 会是空的
                    max_tokens=2000)
                verdict = None
                if isinstance(obj, dict):
                    v = obj.get("same")
                    if isinstance(v, str) and v.strip().isdigit():
                        verdict = int(v.strip()) == 1
                    elif isinstance(v, bool):
                        verdict = v
                    elif isinstance(v, int):
                        verdict = v == 1
                if not verdict:
                    continue          # 模型说不是（或没答上来）→ 候选作废
                return idx, {"aid": None, "bgm": c["id"], "zh": c.get("name_cn"),
                             "title": c.get("name"), "year": None,
                             "matched": name, "src": "net"}
        return idx, None

    def do_amb(item):
        """同名多候选 —— **用模型自己在识别那一步给出的判断去筛**，不再问第二遍。

        模型已经表态了 `season` / `year`，那就是它的判断；这里只是把它自己的
        判断**应用到候选上**（模型说有第 2 季，候选里第 2 季那条才是它指的）。
        不是我在替它猜：没有任何我拍的权重、没有模糊字符串比较。

        筛完仍分不出的，才真的叫模型（而且**批量**叫，10 个单元一次调用，
        省免费档配额）。
        """
        idx, (name, srckind, aids) = item
        infos = []
        for a in aids[:12]:
            r = con.execute("SELECT title,bangumi,zh_name,year,season,episodes,type "
                            "FROM anime WHERE aid=?", (a,)).fetchone()
            if r:
                infos.append((a, r))
        if not infos:
            return idx, None

        res = rows[idx]["result"]
        m_season = res.get("season")
        m_year = str(res.get("year") or "").strip()

        def pick(pool):
            if not pool:
                return None
            aid, r = pool[0]
            return {"aid": aid, "bgm": r[1], "zh": r[2], "title": r[0],
                    "year": r[3], "matched": name, "src": srckind}

        # ① 候选都指向同一个番组计划 id → 元数据一样，谁都不影响结果
        bgms = {r[1] for a, r in infos if r[1]}
        if len(bgms) == 1:
            return idx, pick(infos)
        # ② 用模型自己说的季号筛
        if m_season:
            pool = [x for x in infos if x[1][4] == m_season]
            if len(pool) == 1:
                return idx, pick(pool)
            if pool and len({x[1][1] for x in pool}) == 1:
                return idx, pick(pool)
        # ③ 用模型自己说的年份筛
        if m_year:
            pool = [x for x in infos if str(x[1][3] or "") == m_year]
            if len(pool) == 1:
                return idx, pick(pool)
            if pool and len({x[1][1] for x in pool}) == 1:
                return idx, pick(pool)
        # ④ 分不出 —— 交给批量段
        return idx, None

    # 先跑一遍不叫模型的筛法，看还剩多少真需要问
    leftover = []
    for it in amb:
        i, b = do_amb(it)
        if b:
            plan[i] = b
        else:
            leftover.append(it)
    print("③ 用模型自己给的 season/year 筛掉 %d 个；剩 %d 个真要问模型"
          % (len(amb) - len(leftover), len(leftover)), flush=True)

    def do_batch(chunk):
        """批量问：10 个单元一次调用，省免费档配额。"""
        parts = []
        for n, (idx, (name, srckind, aids)) in enumerate(chunk):
            infos = []
            for a in aids[:8]:
                r = con.execute("SELECT title,bangumi,year,season FROM anime WHERE aid=?",
                                (a,)).fetchone()
                if r:
                    infos.append((a, r))
            cands = "\n".join("   %d) %s（%s 第%s季）" % (i + 1, r[0], r[2] or "?", r[3] or "?")
                              for i, (a, r) in enumerate(infos))
            parts.append("【第 %d 组】目录名：%s\n文件示例：\n%s\n候选：\n%s" % (
                n + 1, rows[idx]["unit"][-60:],
                "\n".join("   " + f[-70:] for f in rows[idx]["files"][:6]), cands))
        obj = ask_json(
            "下面有 %d 组动漫目录，每组的名字在番库里对应多个条目。\n"
            "请为每组选出正确的那个，**只输出组号和序号**，不要写理由。\n\n%s\n\n"
            '只输出 JSON：{"picks": {"1": 2, "2": 1, ...}}' % (len(chunk), "\n\n".join(parts)),
            max_tokens=2500)
        out = []
        picks = (obj or {}).get("picks") or {}
        for n, (idx, (name, srckind, aids)) in enumerate(chunk):
            infos = []
            for a in aids[:8]:
                r = con.execute("SELECT title,bangumi,zh_name,year FROM anime WHERE aid=?",
                                (a,)).fetchone()
                if r:
                    infos.append((a, r))
            if not infos:
                continue
            k = 0
            try:
                k = int(picks.get(str(n + 1), 1)) - 1
            except Exception:
                k = 0
            k = max(0, min(len(infos) - 1, k))
            aid, r = infos[k]
            out.append((idx, {"aid": aid, "bgm": r[1], "zh": r[2], "title": r[0],
                              "year": r[3], "matched": name, "src": srckind}))
        return out

    chunks = [leftover[i:i + 10] for i in range(0, len(leftover), 10)]
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(do_net, i) for i in need_net]
        futs += [ex.submit(do_batch, c) for c in chunks]
        for f in as_completed(futs):
            res2 = f.result()
            if isinstance(res2, tuple):
                i, b = res2
                if b:
                    plan[i] = b
                    if b["bgm"] and b["bgm"] not in meta_map:
                        meta_map[b["bgm"]] = meta.subject(b["bgm"])
            else:
                for i, b in res2:
                    plan[i] = b
                    if b["bgm"] and b["bgm"] not in meta_map:
                        meta_map[b["bgm"]] = meta.subject(b["bgm"])

    # ═══ ③b 翻译段：本地、联网都没挂上元数据的，让模型**翻译**中文名 ═══
    # 规矩：「中文元数据……没有就翻译，实在不行才回退日文或英文」。
    # 所以这里不是"回退成罗马音"就完事，而是先请模型把它译成中文。
    still_none = [i for i, b in enumerate(plan) if b is None]
    print("③b 需要翻译中文名的单元 %d 个" % len(still_none), flush=True)

    def do_translate(idx):
        rec = rows[idx]
        obj = ask_json(
            "这是一部动漫。请用你的知识给出它的**中文名**（大陆通用译名）。\n\n"
            "你从文件名里读到的原名：%s\n"
            "日文原名（你判断的）：%s\n"
            "年份：%s\n"
            "目录里的文件名示例：\n%s\n\n"
            '输出 JSON：{"name_cn": "中文名", "name_ja": "日文原名", '
            '"summary": "两三句话的中文剧情简介", "certain": true 或 false}\n\n'
            "不知道就填空字符串，**不要编**。",
            max_tokens=800)
        return idx, obj

    trans = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in as_completed([ex.submit(do_translate, i) for i in still_none]):
            i, obj = f.result()
            if obj:
                trans[i] = obj
    got_cn = sum(1 for o in trans.values() if (o.get("name_cn") or "").strip())
    print("③b 译出中文名 %d / %d" % (got_cn, len(still_none)), flush=True)

    # ═══ ④ 写库 ═══
    stat = {"listing": 0, "recall": 0, "net": 0, "none": 0}
    n_file = n_oob = 0
    for n, rec in enumerate(rows):
        res = rec["result"]
        best = plan[n]
        srckind = best["src"] if best else "none"
        stat[srckind] += 1
        conf = {"listing": "high", "recall": "low", "net": "low"}.get(srckind, "none")
        sub = meta_map.get(best["bgm"]) if best and best["bgm"] else None
        # 名字的回退链（中文优先 → 翻译 → 才回退日文/英文）
        tr = trans.get(n) or {}
        m_title = (res.get("title") or "").strip()
        name_cn = ((sub or {}).get("name_cn") or (best or {}).get("zh")
                   or (tr.get("name_cn") or "").strip() or None)
        name_ja = ((sub or {}).get("name") or (best or {}).get("title")
                   or (tr.get("name_ja") or "").strip() or (res.get("recall") or "").strip()
                   or m_title or None)
        if not name_cn and not best:
            # 全都没挂上：至少把模型读到的原名显示出来，别让它变成空名
            name_cn = m_title or None
        summary = (sub or {}).get("summary") or (tr.get("summary") or "").strip() or None
        n_ep = sum(1 for e in (res.get("_parsed") or {}).values()
                   if e.get("kind") == "正片")
        cur = con.execute(
            "INSERT OR REPLACE INTO unit (disk,unit,aid,bgm_id,src,conf,matched,"
            "name_cn,name_ja,year,date,summary,poster,season,part,dir_kind,has_main,"
            "n_files,n_ep,reading,notes,problem) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["disk"], rec["unit"],
             best["aid"] if best else None, best["bgm"] if best else None, srckind, conf,
             best["matched"] if best else None,
             name_cn, name_ja,
             ((sub or {}).get("date") or "")[:4] or str((best or {}).get("year") or ""),
             (sub or {}).get("date"), summary,
             ((sub or {}).get("images") or {}).get("large"),
             res.get("season"), res.get("part"), res.get("dir_kind"),
             1 if res.get("has_main") else 0, len(rec["files"]), n_ep,
             (res.get("reading") or "")[:600], (res.get("notes") or "")[:500],
             ("; ".join(res.get("_problems") or []))[:500]))
        uid = cur.lastrowid
        nf = len(rec["files"])
        for k, e in (res.get("_parsed") or {}).items():
            try:
                i = int(k)
            except Exception:
                continue
            if not (1 <= i <= nf):
                n_oob += 1
                continue
            con.execute("INSERT INTO ufile (uid,i,path,ep,kind,what) VALUES (?,?,?,?,?,?)",
                        (uid, i, rec["files"][i - 1], e.get("ep"), e.get("kind"),
                         e.get("what")))
            n_file += 1
        if n % 50 == 0:
            con.commit()
            print("[%3d/%d] %-38s → %-20s %s" % (
                n + 1, len(rows), rec["unit"][-38:],
                ((sub or {}).get("name_cn") or (best or {}).get("title") or "-")[:20], srckind),
                flush=True)
    con.commit()
    print("\n来源统计：" + "  ".join("%s=%d" % (k, v) for k, v in stat.items()))
    print("入库文件条目 %d，丢弃越界 %d" % (n_file, n_oob))
    con.close()
    return 0


# TODO(迁移存疑): 原 resolve2.py 用 SCHEMA_EXTRA 显式 DROP 掉 unit/ufile 再建。
# 这里改成读 db.SCHEMA 再就地替换 CREATE 语句。行为等价（都是先 DROP 再建），
# 但**没在真实数据上跑过**（跑一次要调模型），所以标出来。
