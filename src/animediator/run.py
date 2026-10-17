"""一键跑完整条链。

每一步都是幂等的，中间产物全在 `work/`，所以：
    · 断在哪一步，改完直接重跑那一步（或者 `run --from 那一步`）
    · 想先看某一步要干什么，加 `--dry-run`

════════ 链的顺序不是随便排的 ════════
认片之前必须先有**名字索引**（不然查不到任何东西）；
复核必须在归库**之后**（它是在归库结论上打补丁）；
拆分必须在复核**之后**（复核会把被误判成特典的正片找回来，
这些正片本来进不了拆分队列 —— 队列要在修正后的视图上算）；
生成必须在全部修正**之后**。

⚠️ 这条顺序是踩出来的：早先拆分和溢出重判的目标队列是从**原始识别结果**
   算的，于是复核才被救回来的 unit 永远进不了队列（孤独摇滚总集篇、
   来自深渊 3 部剧场版、进击最终季都是这么漏的）。
"""
from __future__ import annotations

import importlib
import sys
import time

from .config import get as get_cfg

# (步骤名, 说明, 模块:函数, 是不是必须语义层)
STEPS = [
    ("scan",        "扫盘（只读）",                    "scan:main",             False),
    ("identify",    "识别每个目录",                    "identify:main",         True),
    ("resolve",     "归库（有序校验）",                "resolve:main",          True),
    ("reresolve",   "复核归库 v1（看原始路径）",       "reresolve:main",        True),
    ("reresolve-search", "复核归库 v2（加检索）",      "reresolve_search:main", True),
    ("apply",       "把复核结果写回作业库（带备份）",  "apply:main",            False),
    ("recheck",     "复核正片/特典的误判",             "recheck:main",          True),
    ("split",       "拆开一个目录装两季的",            "split:main",            True),
    ("overflow",    "溢出重排（集号装不下）",          "overflow:main",         True),
    ("materialize", "生成虚拟库",                      "materialize:main",      False),
    ("nfo",         "写集级 NFO",                      "nfo:main",              False),
    ("deploy",      "接进 Jellyfin 并扫描",            "deploy:main",           False),
    ("report",      "出报告和清单",                    "report:main",           False),
]

NAMES = [s[0] for s in STEPS]


def main(args=None):
    cfg = get_cfg()
    frm = getattr(args, "from_step", None)
    to = getattr(args, "to_step", None)
    if frm and frm not in NAMES:
        print("--from 不认识：%s\n可选：%s" % (frm, "、".join(NAMES)))
        return 2
    if to and to not in NAMES:
        print("--to 不认识：%s\n可选：%s" % (to, "、".join(NAMES)))
        return 2
    i0 = NAMES.index(frm) if frm else 0
    i1 = NAMES.index(to) if to else len(NAMES) - 1
    if i0 > i1:
        print("--from 在 --to 后面，没有要跑的步骤")
        return 2

    from .providers import Semantic, sem
    s = sem()
    todo = STEPS[i0:i1 + 1]

    need_sem = [n for n, _, _, m in todo if m]
    print("将跑 %d 步：%s" % (len(todo), " → ".join(n for n, _, _, _ in todo)))
    if need_sem and not s.enabled:
        print()
        print("⚠️ 这条链里有 %d 步需要语义层：%s" % (len(need_sem), "、".join(need_sem)))
        print("   现在没配。要么先把 [semantic] 配上，要么用 --from/--to 跳过它们。")
        print("   跑 `animediator doctor` 看当前状态。")
        return 2
    print()

    t_all = time.time()
    for n, (_name, desc, target, needs) in enumerate(todo, 1):
        mod_name, fn_name = target.split(":")
        print("═" * 64)
        print("【%d/%d】%s —— %s" % (n, len(todo), n_name := _name, desc))
        print("═" * 64)
        t0 = time.time()
        try:
            mod = importlib.import_module("." + mod_name, __package__)
        except ImportError as ex:
            print("  模块 %s 没装上：%s" % (mod_name, ex))
            return 2
        rc = getattr(mod, fn_name)(args)
        print("—— %s 用时 %.0f 秒%s" % (_name, time.time() - t0,
                                        "" if rc in (None, 0) else "（返回码 %s）" % rc))
        if rc not in (None, 0):
            print("  这一步返回了非 0，停下来，后面的步骤没跑。")
            print("  中间产物在 %s，修完直接 `animediator run --from %s`。" % (cfg.work, _name))
            return int(rc)
        print()

    print("═" * 64)
    print("整条链跑完，总用时 %.0f 分钟（%.1f 小时）"
          % ((time.time() - t_all) / 60, (time.time() - t_all) / 3600))
    print("虚拟库：%s" % cfg.vfs)
    if (cfg.work / "web" / "index.html").exists():
        print("报告：  %s" % (cfg.work / "web" / "index.html"))
    return 0
