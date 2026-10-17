"""一键跑完整条流程。

这里的目标是**一条命令跑到底，中途不需要人盯着**：

    animediator run

它会自己完成：检查离线数据是否最新（需要就下载）→ 建索引 → 扫盘 →
判定 → 归库 → 修正 → 生成 → NFO → 接入 Jellyfin → 出报告。

════════ 设计要点 ════════

**数据自动更新。** 索引数据来自每周发布的归档，流程第一步会查询当前最新版本，
比本地新就下载。不需要用户去网站找文件、也不需要记得每周手动更新。

**下载失败不阻断。** 本地已有一份可用数据时，更新失败只是保留旧版本继续跑，
并说明用的是哪个版本。只有本地完全没有数据时才中止。

**断点续跑。** 每个步骤的产物都落在 work/ 里。任何一步失败或被中断，
`run --from <步骤>` 从断点继续；判定阶段已完成的单元不会重新调用模型。

**失败即停，不假装成功。** 某一步返回非零就停下并打印续跑命令，绝不跳过
继续往下走 —— 带着残缺的中间产物去生成媒体库，结果比停下更糟糕。

**不需要人回答。** 全程没有交互提问。

════════ 步骤顺序的依据 ════════

判定之前必须有名字索引（否则查不到任何东西）。
复核必须在归库之后（它是在归库结论上打补丁）。
拆分必须在复核之后 —— 复核会把被误判成特典的正片找回来，
这些正片本来进不了拆分队列，队列要在修正后的视图上算。
生成必须在全部修正之后。

这条顺序是实测出来的：早先把拆分队列建在修正之前，
复核才救回来的单元永远进不了队列。
"""
from __future__ import annotations

import importlib
import time

from .config import get as get_cfg

# (步骤名, 说明, 模块:函数, 是否必需语义层, 是否只在需要时执行)
STEPS = [
    ("index",     "检查并更新离线索引",              "bgm.index:main",        False),
    ("scan",      "扫描磁盘",                        "scan:main",             False),
    ("identify",  "判定每个目录的内容",               "identify:main",         True),
    ("resolve",   "归属到 Bangumi 条目",             "resolve:main",          True),
    ("reresolve", "按原始路径复核归属",               "reresolve:main",        True),
    ("apply",     "写回复核结果",                    "apply:main",            False),
    ("recheck",   "复核正片与特典的误判",             "recheck:main",          True),
    ("split",     "拆分包含多季的目录",               "split:main",            True),
    ("overflow",  "重排集号溢出的分组",               "overflow:main",         True),
    ("materialize", "生成输出目录",                  "materialize:main",      False),
    ("nfo",       "写入集级 NFO",                    "nfo:main",              False),
    ("deploy",    "接入 Jellyfin 并扫描",            "deploy:main",           False),
    ("report",    "生成报告与清单",                  "report:main",           False),
]

NAMES = [s[0] for s in STEPS]
NEED_SEM = {s[0]: s[3] for s in STEPS}


def main(args=None):
    cfg = get_cfg()
    a = args
    frm = getattr(a, "from_step", None) if a is not None else None
    to = getattr(a, "to_step", None) if a is not None else None
    skip_deploy = bool(getattr(a, "no_deploy", False)) if a is not None else False
    skip_sem = bool(getattr(a, "no_semantic", False)) if a is not None else False

    for v, label in ((frm, "--from"), (to, "--to")):
        if v and v not in NAMES:
            print("%s 不认识：%s" % (label, v))
            print("可选：" + "、".join(NAMES))
            return 2

    i0 = NAMES.index(frm) if frm else 0
    i1 = NAMES.index(to) if to else len(NAMES) - 1
    if i0 > i1:
        print("--from 在 --to 之后，没有要跑的步骤")
        return 2
    todo = STEPS[i0:i1 + 1]

    if skip_deploy:
        todo = [s for s in todo if s[0] != "deploy"]

    # 语义层不可用时，只砍掉需要它的步骤，并把被依赖的步骤降级处理
    from .providers import sem
    s = sem()
    if not s.enabled:
        need = [x[0] for x in todo if x[3]]
        if need and not skip_sem:
            print("这条流程里有 %d 步需要语言模型：%s" % (len(need), "、".join(need)))
            print()
            print("两种办法：")
            print("  1. 配置 [semantic]（animediator doctor 会说明要填什么）")
            print("  2. 先把已有判定跑完，跳过需要模型的步骤：")
            print("     animediator run --no-semantic")
            print()
            print("若 work/ 里已经有完整的判定结果，用第 2 种即可。")
            return 2
        todo = [x for x in todo if not x[3]]
        if skip_sem:
            print("跳过需要语言模型的步骤：%s"
                  % "、".join(x[0] for x in todo if x[3]) or "（无）")

    print("将执行 %d 步：" % len(todo))
    for n, (name, desc, _, _) in enumerate(todo, 1):
        print("  %2d. %-12s %s" % (n, name, desc))
    print()

    t_all = time.time()
    done = []
    for n, (name, desc, target, _need) in enumerate(todo, 1):
        print("═" * 66)
        print("[%d/%d] %s —— %s" % (n, len(todo), name, desc))
        print("═" * 66)
        t0 = time.time()
        mod_name, fn_name = target.split(":")
        try:
            mod = importlib.import_module("." + mod_name, __package__)
        except ImportError as ex:
            print("  模块 %s 无法加载：%s" % (mod_name, ex))
            return 2

        # nfo 默认是空跑，流程里要真写
        sub_args = a
        if name == "nfo" and a is not None and not getattr(a, "write", False):
            import argparse
            sub_args = argparse.Namespace(**vars(a))
            sub_args.write = True

        try:
            rc = getattr(mod, fn_name)(sub_args)
        except KeyboardInterrupt:
            print("\n中断。已完成：%s" % ("、".join(done) or "无"))
            print("继续：animediator run --from %s" % name)
            return 130

        spent = time.time() - t0
        print()
        print("—— %s 完成，用时 %s" % (name, _dur(spent)))
        if rc not in (None, 0):
            print()
            print("这一步返回 %s，停下。" % rc)
            print("已完成：%s" % ("、".join(done) or "无"))
            print("处理完后继续：animediator run --from %s" % name)
            return int(rc)
        done.append(name)
        print()

    total = time.time() - t_all
    print("═" * 66)
    print("全部完成，用时 %s" % _dur(total))
    print("输出目录：%s" % cfg.vfs)
    rep = cfg.work / "web" / "index.html"
    if rep.exists():
        print("收录报告：%s" % rep)
    return 0


def _dur(sec):
    if sec < 90:
        return "%d 秒" % sec
    if sec < 5400:
        return "%.1f 分钟" % (sec / 60)
    return "%.1f 小时" % (sec / 3600)
