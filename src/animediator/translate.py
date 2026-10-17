"""给"没挂上元数据"的单元补中文名/日文名/简介 —— 单独跑，好验证。

产品规矩：「中文元数据……没有就翻译，实在不行才回退日文或英文」。
这些单元在离线番库和番组计划里都没挂上，那就让模型用知识**译**出来，
写进 unit 表；同时回写 bgm_cache 之外的一张 `llm_meta` 表，
下次不用再问（离线优先）。

用法: animediator translate [--limit N]
"""
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import get as get_cfg
from .providers import ask_json, require

cfg = get_cfg()
DB = cfg.db
SRC = cfg.w("brain_v1.jsonl")

PROMPT = """这是一部动漫，我在整理硬盘上的收藏。请用你的知识给出它的中文信息。

你从原始文件名里读到的名字：{title}
日文原名（你判断的）：{recall}
年份：{year}
这个目录整体装的是：{dir_kind}
目录里的原始文件名示例：
{files}

输出 JSON：
{{
  "name_cn": "这部作品的中文名（大陆通用译名）。⚠️ 如果这个目录是某一季 / OVA / 剧场版 / 外传，**名字必须带上那部分** —— 例如「在地下城寻求邂逅是否搞错了什么 第二季」而不是光写「在地下城寻求邂逅是否搞错了什么」。番组计划上每一季都是**独立条目**，不带季号就会指错。",
  "name_ja": "日文原名",
  "summary": "两三句话的中文剧情简介"
}}

⚠️ 想不起来就填空字符串，**不要编**。宁可空着也不要写错的。"""


def main(args=None):
    require()
    limit = None
    if args is not None:
        limit = getattr(args, "limit", None)
    else:
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
    rows = {json.loads(l)["unit"]: json.loads(l) for l in open(SRC, encoding="utf-8")}
    con = sqlite3.connect(DB, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS llm_meta (
        unit TEXT PRIMARY KEY, name_cn TEXT, name_ja TEXT, summary TEXT)""")
    todo = [r[0] for r in con.execute(
        "SELECT unit FROM unit WHERE (name_cn IS NULL OR name_cn='') "
        # ⚠️ 还有一类：名字是模型自己报的（aid 和 bgm_id 都空），
        #    可能是"丹马哈"这种坏音译 —— 拿它去 bgm.tv 搜不出东西。
        #    这些也要重译成通行中文名。
        "   OR (aid IS NULL AND bgm_id IS NULL) ORDER BY unit")]
    if limit:
        todo = todo[:limit]
    print("需要补中文信息的单元 %d 个" % len(todo), flush=True)

    done = {"ok": 0, "cn": 0, "fail": 0}

    def work(u):
        rec = rows.get(u)
        if not rec:
            return u, None
        res = rec["result"]
        obj = ask_json(PROMPT.format(
            title=res.get("title") or "?", recall=res.get("recall") or "无",
            year=res.get("year") or "未知", dir_kind=res.get("dir_kind") or "?",
            files="\n".join("   " + f[-72:] for f in rec["files"][:8])),
            max_tokens=900)
        return u, obj

    with ThreadPoolExecutor(max_workers=3) as ex:
        for f in as_completed([ex.submit(work, u) for u in todo]):
            u, obj = f.result()
            done["ok"] += 1
            if not obj:
                done["fail"] += 1
                continue
            cn = (obj.get("name_cn") or "").strip()
            ja = (obj.get("name_ja") or "").strip()
            su = (obj.get("summary") or "").strip()
            if cn:
                done["cn"] += 1
            con.execute("INSERT OR REPLACE INTO llm_meta VALUES (?,?,?,?)", (u, cn, ja, su))
            con.execute("""UPDATE unit SET
                name_cn = COALESCE(NULLIF(?, ''), name_cn),
                name_ja = COALESCE(NULLIF(?, ''), name_ja),
                summary = COALESCE(NULLIF(?, ''), summary)
                WHERE unit = ?""", (cn, ja, su, u))
            con.commit()
            print("[%2d/%d] %-46s → %s" % (done["ok"], len(todo), u[-46:], cn or "(空)"),
                  flush=True)

    print("\n完成 %d，译出中文名 %d，失败 %d" % (done["ok"], done["cn"], done["fail"]))
    n = con.execute("SELECT COUNT(*) FROM unit WHERE name_cn IS NOT NULL AND name_cn!=''").fetchone()[0]
    s = con.execute("SELECT COUNT(*) FROM unit WHERE summary IS NOT NULL AND summary!=''").fetchone()[0]
    t = con.execute("SELECT COUNT(*) FROM unit").fetchone()[0]
    print("现在有中文名 %d / %d，有简介 %d / %d" % (n, t, s, t))
    con.close()


if __name__ == "__main__":
    main()
