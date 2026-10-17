"""识别层 v2 —— 把**每个目录当成一个小项目**，让模型通盘整理。

════════ 为什么重写（实测暴露的病根）════════
v1 的错不在于模型能力，在于**任务切法**：我让模型对着一份清单逐条填空
（`{"行号":"正136"}`）。面对 200+ 条时它只能退化成"填充模式" ——
没有全局视野，不知道这个目录整体是什么，就只能一条条猜，猜不动就照抄格式。
所以 v1 才会出现 245 条 `正136/正137`（照抄我的示例）。

v2 的改法：
  ① **先通读，再逐条**。JSON 里 `reading` 排在 `files` **前面** ——
     模型是自左向右生成的，先写通读结论，后面的逐条判断就建立在自己
     刚才那个整体判断之上（先知道"这是个无字幕片头片尾目录"，
     再逐条定位就不会乱猜）。
  ② **值里没有 `正` 这个前缀**。v1 的 `"正136"` 是个可被照抄的 token 形状；
     v2 里正片就是**纯数字** `"5"`，特典以 `特` 开头。想抄也没得抄。
  ③ **原始文件名一个字都不切**（v1 也是，这条保持）。方括号里的番名、
     字幕组、技术标记全部原样给模型 —— 那正是它判断的依据。
  ④ 身份只让模型**报它读到的东西**：清单里真实出现过的名字排第一，
     它自己记忆里的名字单独放一个字段（只在第一个查不中时才用）。
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import get as get_cfg
from .providers import ask_json, require


def _out():
    """识别结果落哪。取配置而不是模块级常量 —— 配置是运行时才装配的。"""
    return get_cfg().brain_jsonl
LOCK = threading.Lock()
DONE_KEYS = set()


PROMPT = """你是一个动漫库整理员。下面交给你**一个目录项目**，请把这个目录通盘整理出来。

目录名（原样给你，没有做任何加工）：

{name}

⚠️ **目录名本身就在告诉你这个目录装的是什么**，这是最强的线索，先看它：
   `SPs` / `特典` / `Tokuten` / `Omake` / `Bonus` / `映像特典` → 装的是特典
   `menu` / `Menu` / `Menu_` → 装的是蓝光菜单
   `NCOP&NCED` / `NC` / `Creditless` → 装的是**无字幕片头/片尾**
   `PV` / `CM` / `Spot` / `Trailer` / `Preview` → 装的是宣传影像
   `Lite` / `Mini Anime` / `SD` → 装的是短篇
   没有这类字样的，才是正常装正片的目录。

这个目录下的全部文件（原始文件名，一个字都没改过，方括号里的东西不要忽略）：

{files}

──── 请分两步做 ────

【第一步 · 通读】
把整个清单从头到尾读一遍，想清楚这几件事：
  · 这是哪部作品？—— 文件名里一定有它的名字（通常在方括号里），把它读出来。
  · 结合**目录名**和文件内容判断：这个目录**整体**装的是什么？
  · 文件是怎么编号的？编号规律是什么？
  · 有没有哪几个文件跟其它明显不是一类？
把这些结论写进 `reading`，并且**明确表态**：这个目录里到底有没有正片。

【第二步 · 逐条定位】
带着上面的整体判断，再一个个看文件。

⚠️ **自洽要求**：你的表态和逐条判断必须一致 ——
   如果表态是"没有正片"，那 `files` 里就**不该出现任何纯数字**，全部以「特」开头。
   如果表态是"有正片"，那正片那几条才填数字。两者矛盾的话，说明你第一步读错了，重读。

──── 输出 JSON ────

{{
  "reading": "你通读整个清单后的结论（这个目录是什么作品、整体装了什么、怎么编号）",
  "dir_kind": "这个目录整体属于：正片目录 / 特典目录 / 混合目录",
  "has_main": true 或 false（这个目录里到底有没有正片。必须表态，不许含糊）,
  "title": "**作品名本身** —— 从原始文件名里认出来。注意发布组标签（`[VCB-Studio]`、`[Nekomoe kissaten&VCB-Studio]`、`[DMG&VCB-Studio]`）和画质标记（`[Ma10p_1080p]`）**不是**作品名的一部分，别把它们带进来。作品名本身怎么写就怎么写，不要翻译、不要改写。",
  "alt": ["原始文件名里出现的其它写法；没有就空数组"],
  "recall": "**这部作品的日文原名**（汉字/假名原文，例如「葬送のフリーレン」）。这是你的知识，不是从文件名读的。**必须尽力填**；实在想不起来才留空字符串",
  "year": 首播年份，判断不出就填 null,
  "season": 第几季（整数）；没有分季概念就填 1,
  "part": "前后篇标记如 Part 2；没有填 null",
  "files": {{}},
  "notes": "需要说明的写这里（比如目录里混了多部作品）；没有留空"
}}

`title` / `alt` / `recall` 的区别很重要：
  · `title` 和 `alt` 必须**来自文件清单本身**，是你在文件名里真实看到的字。
  · `recall` 是你凭知识另外想到的名字，**可以不是**文件名里的。分开写。

`files` 怎么填（**每个文件都要有一条，一个都不能漏**）：
  键 = 上面清单的**行号**（1、2、3…）
  值 = 三选一：
    · **一个纯数字**，例如 `"5"` —— 这是正片，数字是**第几集**。
    · **以「特」开头**，例如 `"特CM 宣传片"` —— 这是特典，后面写它是什么。
    · **以「疑」开头**，例如 `"疑看不出集号"` —— 拿不准的，写清为什么。

⚠️ `files` 字段**故意留空** —— 没有示例答案给你抄，键和值都得按你实际读到的填。

⚠️ 集号必须从**文件名里读**，跟行号毫无关系：
   清单里第 15 个文件完全可能是第 3 集，也完全可能是特典。
   文件名里的 `1080p` `x265` `Ma10p` `FLAC` `BDRip` `HEVC` `10bit` `VFR` 都**不是**集号。

⚠️ 这些**是特典**，不是集号：
   `[CM01]` `[PV]` `[NCOP]` `[NCED]` `[menu]` `[Tokuten]` `[Lite]` `[Eyecatch]`
   `[IV01]` `[Omake]` `[Picture Drama]` `[Live]` `[SP]` `[Preview]` `[Trailer]`

⚠️ 有些目录**只有特典没有正片**（纯 `PV/`、纯 `menu/`、纯 `NCOP&NCED/` 目录）。
   那就全部填 `特…`，一个正片都没有 —— **这是对的，不要为了凑数硬填集号**。

只输出 JSON，不要别的文字。"""


def file_lines(entries):
    items = []
    for rel, files in entries:
        for f in files:
            p = f if rel in (".", "") else os.path.join(rel, f)
            items.append(p.lstrip("./"))
    return items


def parse_entry(v):
    """v2 格式：`"5"`=正片第5集 / `"特CM"`=特典 / `"疑…"`=存疑。

    ⚠️ 实测踩过（严重）：这里原来只认 `s.isdigit()`，于是模型写出的
       **区间集号** `"296-297"`（一个文件装两集）被判成"读不出"直接丢弃 ——
       文件就这么凭空"漏"了。模型答对了，是我的解析器把它扔了。
       这跟 JSON 白名单是同一个病：**代码在替模型否决输出**。
       现在：带数字就取第一个数字当集号，**原值照抄进 what 一个字节不丢**。
    """
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s.startswith("特"):
        return {"kind": "特典", "ep": None, "what": s[1:].strip() or "特典"}
    if s.startswith("疑"):
        return {"kind": "存疑", "ep": None, "what": s[1:].strip() or "存疑"}
    m = re.search(r"\d+", s)
    if m:
        # 纯数字、区间（296-297）、多段（1,2 / 5+6）都走这里
        return {"kind": "正片", "ep": int(m.group()),
                "what": None if s == m.group() else s}
    return None


def check(obj, n_files):
    """只校验格式并报告，**一律不改模型写的内容**。"""
    probs = []
    fl = obj.get("files")
    if not isinstance(fl, dict):
        return ["files 字段缺失或不是对象"]
    keys = set()
    for k, v in fl.items():
        if not str(k).isdigit():
            probs.append("键不是行号: %r" % k)
            continue
        i = int(k)
        if not (1 <= i <= n_files):
            probs.append("行号越界: %d" % i)
            continue
        keys.add(i)
        if parse_entry(v) is None:
            probs.append("行号 %d 的值读不出: %r" % (i, v))
    miss = [i for i in range(1, n_files + 1) if i not in keys]
    if miss:
        probs.append("有 %d 个文件没出现在 files 里（行号 %s…）" % (len(miss), miss[:8]))
    # 自洽性：表态 vs 实际填的 —— **只报告，不改判**
    n_digit = sum(1 for k, v in fl.items()
                  if str(k).isdigit() and isinstance(v, str) and v.strip().isdigit())
    hm = obj.get("has_main")
    if hm is False and n_digit:
        probs.append("自相矛盾：表态「没有正片」却填了 %d 条数字" % n_digit)
    if hm is True and n_digit == 0:
        probs.append("自相矛盾：表态「有正片」却一条数字都没填")
    return probs


def identify_unit(u):
    files = file_lines(u["entries"])
    listing = "\n".join("%d. %s" % (i + 1, p) for i, p in enumerate(files))
    obj = ask_json(PROMPT.format(name=u["name"], files=listing), max_tokens=4000)
    if obj is None:
        return None
    obj["_problems"] = check(obj, len(files))
    parsed = {}
    for k, v in (obj.get("files") or {}).items():
        if str(k).isdigit():
            e = parse_entry(v)
            if e:
                parsed[int(k)] = e
    obj["_parsed"] = parsed
    return obj


def load_done():
    done = set()
    out = _out()
    if os.path.exists(out):
        for line in open(out, encoding="utf-8"):
            try:
                done.add(json.loads(line)["key"])
            except Exception:
                continue
    return done


def worker(u, fo, counter):
    key = "%s|%s" % (u["disk"], u["name"])
    if key in DONE_KEYS:
        return None
    obj = identify_unit(u)
    with LOCK:
        counter["done"] += 1
        if obj is None:
            counter["fail"] += 1
            print("[%3d/%d] 失败 %s" % (counter["done"], counter["total"], u["name"][:50]))
            return None
        rec = {"key": key, "disk": u["disk"], "unit": u["name"],
               "files": file_lines(u["entries"]), "result": obj}
        fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fo.flush()
        counter["ok"] += 1
        p = obj["_parsed"]
        n_ep = sum(1 for x in p.values() if x["kind"] == "正片")
        n_ex = sum(1 for x in p.values() if x["kind"] == "特典")
        n_un = sum(1 for x in p.values() if x["kind"] == "存疑")
        print("[%3d/%d] %-44s 正%3d 特%3d 疑%2d %s | %s" % (
            counter["done"], counter["total"], u["name"][-44:], n_ep, n_ex, n_un,
            ("问题%d" % len(obj["_problems"])) if obj["_problems"] else "",
            (obj.get("title") or "")[:26]))
    return obj


def main(args=None):
    require()                      # 没配语义层就带着配置说明炸掉，别静默跑成空结果
    cfg = get_cfg()
    workers = int(cfg.semantic.get("workers", 6))
    if args is not None and getattr(args, "workers", None):
        workers = args.workers
    elif "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    units = json.load(open(cfg.units_json, encoding="utf-8"))
    only = getattr(args, "only", None) if args is not None else None
    if only is None and "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    if only:
        units = [u for u in units if only in u["name"]]
        print("筛选后 %d 个单元" % len(units))
    limit = getattr(args, "limit", None) if args is not None else None
    if limit:
        units = units[:int(limit)]
        print("小批试跑：只处理前 %d 个单元" % len(units))
    global DONE_KEYS
    DONE_KEYS = load_done()
    todo = [u for u in units if "%s|%s" % (u["disk"], u["name"]) not in DONE_KEYS]
    print("共 %d 单元，已完成 %d，待跑 %d，模型 %s，并发 %d" % (
        len(units), len(DONE_KEYS), len(todo),
        cfg.semantic.get("model") or "-", workers))
    counter = {"done": 0, "total": len(todo), "ok": 0, "fail": 0}
    t0 = time.time()
    with open(_out(), "a", encoding="utf-8") as fo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for _ in as_completed([ex.submit(worker, u, fo, counter) for u in todo]):
                pass
    print("\n完成 %d/%d（成功 %d 失败 %d），用时 %.1f 分钟" % (
        counter["done"], counter["total"], counter["ok"], counter["fail"],
        (time.time() - t0) / 60))
    return 0
