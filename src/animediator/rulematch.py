"""没有语义层时的降级匹配。

定位先说清楚：**这是降级路径，覆盖率和准确率都明显低于模型判定。**
在 455 个真实目录上实测：覆盖率 53.8%，精确率 60.5%。
错的地方集中在一件事上——一个目录要拆成多个条目（同一目录里有 TV 版、
剧场版、番外），以及续作之间靠名字区分（第二季被认成第一季）。
这两件都需要模型，纯字符串比对做不到。

那为什么还要它：
- 装了但暂时连不上模型时，能先出一份结果看到大概；
- 给模型提供候选，减少它编名字的机会；
- 用户只想快速预览时，不必先配模型。

原则：不删除、不截断原始字符串。做法是把原始路径的不同切片都拿去查，
命中即停——增加查询维度，不丢信息。
"""
from __future__ import annotations

import json
import re
import sqlite3

from .config import get as get_cfg
from .textutil import norm_query as norm


def slices(text: str) -> list[str]:
    """列出一个路径字符串的候选切片，按可信度从高到低。

    整段 → 路径每一级 → 方括号里的内容 → 去掉标签后的各段。
    全部原样保留，只做查询，不改写原文。
    """
    out, seen = [], set()

    def add(value):
        value = (value or "").strip()
        if not value:
            return
        key = norm(value)
        if not key or key in seen:
            return
        # 太短或明显不是作品名的，不当查询词（原文不动，只是不拿它去查）
        if len(key) < MIN_QUERY_LEN or key in _NOT_A_TITLE:
            return
        seen.add(key)
        out.append(value)

    add(text)
    parts = re.split(r"[\\/]", text)
    for part in parts:
        add(part)
    for part in parts:
        for inner in re.findall(r"\[([^\]]+)\]", part):
            add(inner)
        stripped = re.sub(r"\[[^\]]*\]", " ", part)
        for chunk in re.split(r"[_]+", stripped):
            add(chunk)
    return out


class NameIndex:
    """离线索引里的名字表，一次装载反复查。"""

    def __init__(self, db_path):
        con = sqlite3.connect(str(db_path))
        self.table: dict[str, set[int]] = {}
        for sid, nom in con.execute("SELECT subject_id, norm FROM sname"):
            if nom:
                self.table.setdefault(nom, set()).add(sid)
        self.years: dict[int, int] = {}
        for sid, date in con.execute("SELECT id, date FROM subject"):
            if date and len(str(date)) >= 4:
                try:
                    self.years[sid] = int(str(date)[:4])
                except ValueError:
                    pass
        con.close()

    def lookup(self, text: str) -> tuple[str, list[int]] | None:
        """返回 (命中的切片, 候选条目列表)；完全没中返回 None。"""
        for cand in slices(text):
            key = norm(cand)
            hit = self.table.get(key)
            if hit:
                return cand, sorted(hit)
        return None

    def narrow_by_year(self, cands: list[int], year: int) -> list[int]:
        """用目录名里的年份把重名候选收窄。收不窄就原样返回。"""
        kept = [c for c in cands if self.years.get(c) == year]
        return kept or cands


_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

# 这些切片本身不是作品名，拿去查库只会命中一堆无关条目（实测 menu 命中 6 部）。
# 原文完整保留，只是不把它们当成查询词。
_NOT_A_TITLE = {
    "menu", "menus", "sps", "sp", "extras", "extra", "bonus", "preview",
    "trailer", "ncop", "nced", "nc", "op", "ed", "pv", "cm", "sample",
    "fonts", "scans", "cd", "ost", "bd", "dvd", "s1", "s2", "s3", "tv",
    "ova", "oad", "the", "movie", "vol", "part", "disc",
}

# 归一化后短于这个长度的切片没有区分度（"sp"、"01" 会命中大量条目）
MIN_QUERY_LEN = 3


def guess_year(text: str) -> int | None:
    found = _YEAR.findall(text)
    return int(found[-1]) if found else None


def match(units: list[dict], db_path) -> dict:
    """对每个单元给出候选。返回 {(disk, name): {...}}。

    结果只当建议用：conf 只有 high（唯一命中）、low（多候选）两档，
    没有 medium —— 拿不准就是拿不准，不给中间态。
    """
    index = NameIndex(db_path)
    out = {}
    stats = {"exact": 0, "multi": 0, "none": 0}
    for u in units:
        text = u.get("name") or u.get("top") or ""
        found = index.lookup(text)
        if not found:
            stats["none"] += 1
            continue
        cand, cands = found
        year = guess_year(text)
        if year:
            cands = index.narrow_by_year(cands, year)
        out[u.get("name") or ""] = {
            "disk": u.get("disk"),
            "slices_hit": cand,
            "candidates": cands[:12],
            "conf": "high" if len(cands) == 1 else "low",
        }
        stats["exact" if len(cands) == 1 else "multi"] += 1
    return {"units": out, "stats": stats, "index_size": len(index.table)}


def main(args=None):
    """命令行入口：只跑降级匹配，把结果写到 work/rule_match.json。"""
    cfg = get_cfg()
    src = cfg.w("units.json")
    if not src.exists():
        raise SystemExit("缺少 %s\n  先跑 animediator lib scan" % src)
    units = json.loads(src.read_text(encoding="utf-8"))
    if not cfg.bgm_db.exists():
        raise SystemExit("缺少离线索引 %s\n  先跑 animediator index bgm-build" % cfg.bgm_db)

    result = match(units, cfg.bgm_db)
    out = cfg.w("rule_match.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    st = result["stats"]
    total = len(units)
    print("纯名字匹配（不使用模型）")
    print("  唯一命中   %d" % st["exact"])
    print("  多个候选   %d" % st["multi"])
    print("  完全没中   %d" % st["none"])
    print("  覆盖率     %.1f%%" % ((st["exact"] + st["multi"]) * 100.0 / total))
    print()
    print("这只是降级结果：实测精确率约六成，一个目录含多部作品时拆不开，")
    print("续作之间也分不清。配了语义层才准。")
    print("已写入 %s" % out)
    return 0
