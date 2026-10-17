"""按 unit 重新归库 · v2 —— 给模型加**检索能力**（两轮对话）。

════════ v1 的漏网之鱼 ════════
v1 只把「关联表里的条目」当候选。实测寒蝉 289 的关联表里**没有**「礼」（1326）
和「卒」（331033）—— 它们在 bgm 里明明存在，只是关联图连不到。
所以模型看不到正确答案，只能维持原判 → 漏判。

════════ v2 的做法：两轮 ════════
    第 1 轮  模型读 unit（原始路径 + 全部文件名 + 当前条目）
             → 输出它认为的**检索词**：{"q": ["日文原名", "罗马音", "中文名"]}
    检索     代码拿检索词查**本地 bgm 索引**（精确 norm 命中，秒回；
             没命中才退回 LIKE）—— **只做召回，不做判断**
    第 2 轮  模型看：unit 证据 + 当前条目 + 全部关联条目 + 检索结果
             → 输出 {"id": 数字}

这样既不靠"我猜该用哪个正则去切名字"，也不靠截断候选 ——
检索词由模型给，命中与否由事实决定，选哪个仍由模型定。
"""
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import get as get_cfg
from .providers import ask_json, require
from .reresolve import (bgm_conn, fmt_subj, list_files, subj)

cfg = get_cfg()
DB = cfg.db
OUT = cfg.w("reresolve3.jsonl")
PREV = cfg.w("reresolve.jsonl")
WORKERS = int(cfg.semantic.get("workers", 8))


def evidence(u):
    """unit 的原始证据 —— 路径 + 全部文件名，一个字不切。"""
    files = list_files(u["unit"], u["disk"])
    out = ["【发布目录的完整路径】", "  " + u["unit"], ""]
    out.append("【目录里的视频文件】（共 %d 个，原样）" % len(files))
    for f in files[:150]:
        out.append("  " + f)
    if len(files) > 150:
        out.append("  …另外还有 %d 个同类文件" % (len(files) - 150))
    out.append("")
    return "\n".join(out), files


def search_local(b, queries):
    """拿模型给的检索词查本地索引。返回 [(id, name_cn, name, span)]，去重。"""
    hits, seen = [], set()
    for q in queries:
        q = (q or "").strip()
        if len(q) < 2:
            continue
        for r in b.execute(
                "SELECT DISTINCT s.id,s.name_cn,s.name FROM sname m "
                "JOIN subject s ON s.id=m.subject_id WHERE m.norm=? AND s.type=2 LIMIT 12",
                (re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff]", "", q.lower()),)):
            if r["id"] not in seen:
                seen.add(r["id"])
                hits.append(r["id"])
    if len(hits) < 2:
        # 精确没命中 → 退回模糊（慢，约 1.4s，只在必要时用）
        for q in queries[:2]:
            q = (q or "").strip()
            if len(q) < 2:
                continue
            try:
                for r in b.execute(
                        "SELECT DISTINCT s.id FROM sname m JOIN subject s ON s.id=m.subject_id "
                        "WHERE m.txt LIKE ? AND s.type=2 LIMIT 25", ("%" + q + "%",)):
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        hits.append(r["id"])
            except Exception:
                pass
    return hits[:40]


def turn1(u):
    ev, _ = evidence(u)
    p = "\n".join([
        "这是一份日本动画的发布目录。请先判断它大概是**哪一部作品**，",
        "然后给出用来在番组计划（bangumi.tv）里检索它的**检索词**。",
        "",
        ev,
        "【要求】",
        "给出 2~4 个检索词，尽量覆盖：日文原名、罗马音写法、中文译名。",
        "检索词要尽量短、去掉季号之外的修饰；如果目录名里带了季号或剧场版序号，",
        "把季号/序号**保留在检索词里**（番组计划上每一季是独立条目）。",
        '只回答一个 JSON 对象，形如 {"q": ["词1", "词2"]}。不要输出解释。',
    ])
    r = ask_json(p, max_tokens=2000)
    if isinstance(r, dict):
        q = r.get("q")
        if isinstance(q, list):
            return [str(x) for x in q if x]
        if isinstance(q, str):
            return [q]
    return []


def turn2(u, b, hits):
    ev, _ = evidence(u)
    lines = [ev]
    lines.append("【当前被归到的条目（可能不对）】")
    lines.append(fmt_subj(b, u["bgm_id"], "当前：") or "  （无）")
    lines.append("")

    rel = [r["related_id"] for r in b.execute(
        "SELECT related_id FROM subject_relation r JOIN subject s ON s.id=r.related_id "
        "WHERE r.subject_id=? AND s.type=2", (u["bgm_id"],))]
    lines.append("【同系列的其他条目】（前传/续集/剧场版/不同季，都是独立条目；已全部列出）")
    if rel:
        for cid in rel:
            t = fmt_subj(b, cid)
            if t:
                lines.append(t)
    else:
        lines.append("  （没有关联条目）")
    lines.append("")

    if hits:
        lines.append("【用检索词在番组计划里搜到的条目】")
        for cid in hits:
            t = fmt_subj(b, cid)
            if t:
                lines.append(t)
        lines.append("")

    lines.append("【要求】")
    lines.append("判断这个发布目录到底属于上面哪一个条目，用它的 id 回答。")
    lines.append("线索用法：")
    lines.append("  · 目录名或子目录名里的集号范围（如「100-150」「(221-252)」）是发布者标注的；")
    lines.append("    如果它落在某个候选条目的集号区间里、而不落在当前条目里，就该归那个候选。")
    lines.append("  · 特典 / NCOP / NCED / PV / menu / SPs 这类目录，属于它所属那部正片的条目。")
    lines.append("  · 剧场上映、OVA、外传、不同季在番组计划上都是独立条目，不要硬塞进当前条目。")
    lines.append("")
    lines.append('只回答一个 JSON 对象，形如 {"id": 数字}。')
    lines.append('无法确定就回答 {"id": 0}。不要输出解释。')
    r = ask_json("\n".join(lines), max_tokens=2000)
    if isinstance(r, dict):
        try:
            return int(r.get("id"))
        except (TypeError, ValueError):
            return None
    return None


def one(u):
    b = bgm_conn()
    try:
        qs = turn1(u)
        hits = search_local(b, qs) if qs else []
        new = turn2(u, b, hits)
        return dict(uid=u["uid"], unit=u["unit"], disk=u["disk"], kind=u["dir_kind"],
                    n=u["n_files"], old=u["bgm_id"], new=new, q=qs,
                    n_hits=len(hits))
    except Exception as e:
        return dict(uid=u["uid"], unit=u["unit"], old=u["bgm_id"], new=None,
                    why=str(e)[:120])
    finally:
        b.close()


def main(args=None):
    require()
    a = sqlite3.connect(DB)
    a.row_factory = sqlite3.Row
    units = [dict(r) for r in a.execute(
        "SELECT uid,unit,disk,bgm_id,name_cn,dir_kind,n_files FROM unit "
        "WHERE bgm_id IS NOT NULL ORDER BY uid")]

    if args is not None:
        only = getattr(args, "only", None)
        redo = bool(getattr(args, "redo", False))
    else:
        only = os.environ.get("AMI_ONLY")
        redo = os.environ.get("AMI_RECHECK") == "kept"
    if only:
        units = [u for u in units if only in u["unit"]]

    if redo and os.path.exists(PREV):
        kept = set()
        for ln in open(PREV, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if not d.get("new") or int(d["new"]) == int(d.get("old") or 0):
                kept.add((d["uid"], d["unit"]))
        units = [u for u in units if (u["uid"], u["unit"]) in kept]
        print("（只重查上一轮维持原判的 %d 个）" % len(units))

    print("待复判 unit：%d 个，并发 %d" % (len(units), WORKERS))
    done = {}
    if os.path.exists(OUT):
        for ln in open(OUT, encoding="utf-8"):
            try:
                d = json.loads(ln)
                done[(d["uid"], d["unit"])] = d
            except Exception:
                pass
        print("已完成 %d 个（续跑）" % len(done))

    todo = [u for u in units if (u["uid"], u["unit"]) not in done]
    f = open(OUT, "a", encoding="utf-8")
    n = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(one, u): u for u in todo}
        for fut in as_completed(futs):
            d = fut.result()
            done[(d["uid"], d["unit"])] = d
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            f.flush()
            n += 1
            if n % 20 == 0:
                ch = sum(1 for x in done.values() if x.get("new") and x["new"] != x["old"])
                print("  %d/%d  已改判 %d" % (n, len(todo), ch), flush=True)
    f.close()

    b = bgm_conn()
    changed = [d for d in done.values() if d.get("new") and d["new"] != d["old"]]
    unknown = [d for d in done.values() if not d.get("new")]
    print("\n总结：共 %d 个 unit" % len(done))
    print("  维持原判 %d" % (len(done) - len(changed) - len(unknown)))
    print("  **改判 %d**" % len(changed))
    print("  未定 %d" % len(unknown))
    print()
    for d in changed[:40]:
        o, nw = subj(b, d["old"]), subj(b, d["new"])
        print("  %s" % d["unit"][-84:])
        print("      %s → %s" % (
            ("《%s》" % ((o["name_cn"] or o["name"])[:20])) if o else d["old"],
            ("《%s》" % ((nw["name_cn"] or nw["name"])[:20])) if nw else d["new"]))
    b.close()


if __name__ == "__main__":
    main()
