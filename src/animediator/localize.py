"""把非中文的作品简介翻成中文，结果缓存起来给 NFO 用。

bgm 的 summary 只有一份，写的人用什么语言就是什么语言。实测这一批 196 部里
147 部是中文、37 部日文、4 部混合、1 部英文，还有 7 部空。也就是说两成多的
条目直接写进 NFO 就是外语简介。

翻译结果存 work/summary_zh.json，键是 bgm id。缓存是为了不重复花钱花时间：
第二次跑只会处理新出现的条目。

    animediator lib localize --stats   只统计，不调模型
    animediator lib localize           翻译缺的那些
    animediator lib localize --force   连已有缓存一起重翻

需要语义层。没有配置时会直接报错退出，不会静默跳过 —— 否则跑完看着成功、
NFO 里还是日文。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

from .config import FLAGS, get as get_cfg

CJK = re.compile(r"[\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff]")
LATIN = re.compile(r"[A-Za-z]")

PROMPT = (
    "把下面这段作品简介翻译成简体中文。要求：\n"
    "1. 只输出译文，不要任何解释、标题或引号；\n"
    "2. 保留人名的常见中文译名，专有名词没有通行译名就音译；\n"
    "3. 不要省略内容，不要加译者的主观评论；\n"
    "4. 原文里有换行就保留换行。\n\n"
    "原文：\n%s"
)


def detect(summary):
    """判断简介是什么语言。返回 中文 / 日文 / 英文 / 混合 / 空。"""
    if not summary or not summary.strip():
        return "空"
    c = len(CJK.findall(summary))
    k = len(KANA.findall(summary))
    a = len(LATIN.findall(summary))
    if k > c:
        return "日文"
    if c > max(k, a) * 1.2:
        return "中文"
    if a > c:
        return "英文"
    return "混合"


def _entries(cfg):
    """虚拟库里实际用到的条目 → {bgm_id: 目录名}。"""
    out = {}
    if not cfg.vfs.exists():
        return out
    for d in sorted(os.listdir(cfg.vfs)):
        ini = os.path.join(cfg.vfs, d, "bangumi.ini")
        if not os.path.isfile(ini):
            continue
        try:
            m = re.search(r"^ID=(\d+)", open(ini, encoding="utf-8").read(), re.M)
        except OSError:
            continue
        if m:
            out[int(m.group(1))] = d
    return out


def main(args=None):
    cfg = get_cfg()
    cache_path = cfg.w("summary_zh.json")
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}

    entries = _entries(cfg)
    if not entries:
        print("虚拟库里没有带 bangumi.ini 的目录，先跑 materialize。")
        return 2

    con = sqlite3.connect("file:%s?mode=ro" % cfg.bgm_db, uri=True)
    rows = {}
    q = ",".join("?" * len(entries))
    for sid, summary in con.execute(
            "SELECT id, summary FROM subject WHERE id IN (%s)" % q,
            list(entries)):
        rows[sid] = summary or ""
    con.close()

    buckets = {}
    for sid, summary in rows.items():
        buckets.setdefault(detect(summary), []).append(sid)
    print("虚拟库 %d 部，简介语言：" % len(entries))
    for k in ("中文", "日文", "英文", "混合", "空"):
        if buckets.get(k):
            print("  %-4s %d 部" % (k, len(buckets[k])))

    todo = [sid for k in ("日文", "英文", "混合") for sid in buckets.get(k, [])]
    force = bool(getattr(args, "force", False))
    if force:
        todo = [sid for k in ("日文", "英文", "混合", "空") for sid in buckets.get(k, [])]
    pending = [s for s in todo if force or str(s) not in cache]
    limit = getattr(args, "limit", None)
    if limit:
        pending = pending[:limit]
    print("\n需要翻译 %d 部，其中已有缓存 %d 部、这次要翻 %d 部"
          % (len(todo), len(todo) - len(pending), len(pending)))

    if getattr(args, "stats", False) or not pending:
        if pending and getattr(args, "stats", False):
            print("（--stats 只看统计，不调模型）")
        return 0

    from .providers import require, ask
    require()
    if FLAGS.dry_run:
        print("[dry-run] 不调模型、不写缓存。")
        return 0

    ok = fail = 0
    for i, sid in enumerate(pending, 1):
        src = rows.get(sid) or ""
        if not src.strip():
            continue
        out = ask(PROMPT % src, max_tokens=1200)
        if not out:
            fail += 1
            print("[%3d/%d] %s 没拿到译文" % (i, len(pending), entries.get(sid, sid)[:30]))
            continue
        text = out.strip().strip('"').strip("「」").strip()
        # 模型有时会把提示词复述一遍，取第一段中文最长的部分
        if len(text) > 20 and "翻译" in text[:10]:
            text = text.split("\n", 1)[-1].strip()
        if not CJK.search(text):
            fail += 1
            print("[%3d/%d] %s 返回里没有中文，跳过"
                  % (i, len(pending), entries.get(sid, sid)[:30]))
            continue
        cache[str(sid)] = text
        ok += 1
        print("[%3d/%d] %s" % (i, len(pending), entries.get(sid, sid)[:40]))
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n翻好 %d 部，失败 %d 部。缓存 %s"
          % (ok, fail, cache_path))
    print("重跑 animediator lib nfo --write 让 NFO 用上中文简介。")
    return 0 if fail == 0 else 1
