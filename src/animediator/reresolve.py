"""按 unit 重新归库 —— 让模型看**原始路径**（含集号标注）确认每一条的身份。

════════ 为什么推倒重来 ════════
v2 归库是按 unit 逐条查名字、命中即停。问题在**没有约束**：
    · 不同季/剧场版被塞进同一个 bgm 条目（火影 3425 挂了 26 个 unit，
      其中包括疾风传的 13 个，而 3425 只到 220 集；实测目录标着 221-592）
    · 相似名造成跨作品错配（憧憬成为魔法少女 → 魔法少女小圆）
实测：104/121 个 bgm 条目各挂了多个 unit，其中 19 个确认区间超范围。

════════ 关键改进：给模型**原始证据** ════════
之前只给模型"文件名读出来的番名"。现在给它的是一整个小项目：
    ① unit 的完整原始路径（发布组、子目录名 —— 里面常常直接写着集号范围，
       如 `TV S2 Naruto Shippuuden/001-032(221-252)`、`[S3][100-150TV]`）
    ② 该 unit 下的全部文件名（**原样，一个字不切**）
    ③ 当前归到的 bgm 条目：中文名 / 原名 / 集号区间 / 集数
    ④ 该条目的关联条目（前传/续集/同系列）及各自区间 —— 这是找"正确的那一季"的线索

输出只要一个 id：`{"id": 数字}` 或 `{"id": 0}`（无法确定）。

⚠️ 规矩（血训）：
    · prompt 里**不给具体示例值** —— 给了模型就照抄
    · 判断类输出**只要数字**，不写理由（写理由会截断）
    · **代码不替模型否决输出**
"""
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import get as get_cfg
from .providers import ask_json, require, sem

cfg = get_cfg()
DB = cfg.db
BGMDB = cfg.bgm_db
REAL = {k: (m.get("root") or "") for k, m in (cfg.media or {}).items()}
VIDEO = (".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".wmv", ".flv", ".rmvb", ".mov")
OUT = cfg.w("reresolve.jsonl")
# 第二轮用：只重查上一轮「维持原判」的那些（候选补全后可能改判）
PREV = cfg.w("reresolve.jsonl")
WORKERS = int(cfg.semantic.get("workers", 8))


def bgm_conn():
    c = sqlite3.connect(BGMDB)
    c.row_factory = sqlite3.Row
    return c


def subj(b, sid):
    return b.execute("SELECT id,name,name_cn,date,platform FROM subject WHERE id=?",
                     (sid,)).fetchone()


def rng(b, sid):
    r = b.execute("SELECT MIN(sort),MAX(sort),COUNT(*) FROM episode "
                  "WHERE subject_id=? AND sort>0", (sid,)).fetchone()
    return (int(r[0]), int(r[1]), r[2]) if r and r[0] is not None else None


def fmt_subj(b, sid, tag=""):
    s = subj(b, sid)
    if not s:
        return None
    rr = rng(b, sid)
    span = "集号 %d-%d（%d 集）" % rr if rr else "无章节数据"
    return "  %s id=%d《%s》原名 %s  %s  %s" % (
        tag, s["id"], (s["name_cn"] or "（无中文名）")[:30],
        (s["name"] or "")[:34], (s["date"] or "")[:4], span)


def list_files(unit, disk):
    base = os.path.join(REAL.get(disk, ""), unit)
    out = []
    for root, dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in VIDEO:
                out.append(("" if rel == "." else rel + "/") + f)
    return sorted(out)


def build_prompt(u, b):
    files = list_files(u["unit"], u["disk"])
    lines = []
    lines.append("这是一份日本动画的发布目录。请判断**它属于哪一部作品**。")
    lines.append("")
    lines.append("【目录的完整原始路径】")
    lines.append("  " + u["unit"])
    if u["disk"]:
        lines.append("  所在盘：%s" % u["disk"])
    lines.append("")
    lines.append("【目录里的视频文件】（共 %d 个，原样列出）" % len(files))
    for f in files[:120]:
        lines.append("  " + f)
    if len(files) > 120:
        lines.append("  …外加 %d 个" % (len(files) - 120))
    lines.append("")

    lines.append("【当前被归到的条目（可能不对）】")
    cur = fmt_subj(b, u["bgm_id"], "当前：")
    lines.append(cur or "  （无）")
    lines.append("")

# ⚠️ 候选**全部列出，不截断**。
#    上一版这里写的是 `LIMIT 30` + `cand[:14]`，结果寒蝉 289 底下
#    「卒」「礼」这两个真实条目压根没进候选列表，模型看不到就只能维持原判。
#    血的规矩：截断候选 = 把正确答案扔掉。宁可 prompt 长。
    cand = []
    for r in b.execute(
            "SELECT r.related_id FROM subject_relation r "
            "JOIN subject s ON s.id=r.related_id "
            "WHERE r.subject_id=? AND s.type=2", (u["bgm_id"],)):
        cand.append(r["related_id"])
    lines.append("【同系列的其他条目】（前传/续集/剧场版/不同季，都是独立条目）")
    if cand:
        for cid in cand:
            t = fmt_subj(b, cid)
            if t:
                lines.append(t)
    else:
        lines.append("  （没有关联条目）")
    lines.append("")

    # 同名/近名候选（本地索引查一次）
    nm = (u["name_cn"] or "").strip()
    if nm:
        rows = b.execute(
            "SELECT s.id,s.name_cn,s.name FROM sname m JOIN subject s ON s.id=m.subject_id "
            "WHERE m.norm=? AND s.type=2 LIMIT 8", (re.sub(r"\W+", "", nm.lower()),)).fetchall()
        extra = [r["id"] for r in rows if r["id"] != u["bgm_id"]]
        if extra:
            lines.append("【名字相同或相近的其他条目】")
            for cid in extra:          # 同样不截断
                t = fmt_subj(b, cid)
                if t:
                    lines.append(t)
            lines.append("")

    lines.append("【要求】")
    lines.append("判断这个发布目录属于上面哪一个条目。")
    lines.append("注意：目录名里的集号范围（例如「100-150」「(221-252)」）是发布者标注的，")
    lines.append("     如果它落在某个候选条目的集号区间里、而不在当前条目里，那它就该归那个候选。")
    lines.append("注意：特典 / NCOP / NCED / PV / menu 这类目录，属于它所属的那部正片的条目。")
    lines.append("")
    lines.append('只回答一个 JSON 对象，形如 {"id": 数字}。')
    lines.append("id 用上面列出的某个条目的 id；如果确实无法确定，回答 {\"id\": 0}。")
    lines.append("不要输出任何解释。")
    return "\n".join(lines)


def one(u, idx, total):
    b = bgm_conn()
    try:
        p = build_prompt(u, b)
        r = ask_json(p, max_tokens=2000)
        if not isinstance(r, dict):
            return dict(uid=u["uid"], unit=u["unit"], old=u["bgm_id"], new=None,
                        why="模型没给出 JSON")
        v = r.get("id")
        try:
            new = int(v)
        except (TypeError, ValueError):
            new = None
        return dict(uid=u["uid"], unit=u["unit"], disk=u["disk"],
                    kind=u["dir_kind"], n=u["n_files"],
                    old=u["bgm_id"], new=new)
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

    # 只重查上一轮「维持原判」的（new 为空或等于 old）
    if redo:
        kept = set()
        if os.path.exists(PREV):
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
        futs = {ex.submit(one, u, i, len(todo)): u for i, u in enumerate(todo)}
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
    print("  模型无法确定 %d" % len(unknown))
    print()
    print("改判明细（前 30）：")
    for d in changed[:30]:
        o, nw = subj(b, d["old"]), subj(b, d["new"])
        print("  %s" % d["unit"][-80:])
        print("      %s → %s" % (
            ("《%s》%s" % ((o["name_cn"] or o["name"])[:16], d["old"])) if o else d["old"],
            ("《%s》%s" % ((nw["name_cn"] or nw["name"])[:16], d["new"])) if nw else d["new"]))
    b.close()


if __name__ == "__main__":
    main()
