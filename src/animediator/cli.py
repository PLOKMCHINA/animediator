"""命令行入口。

    animediator <命令> [选项]

命令按**流水线顺序**排，从上到下就是一次完整整理要走的路：

    ① 建库   bgm build / bgm merge / bgm people      一次性，离线索引
    ② 摸底   scan                                     扫盘，只读
    ③ 认片   identify                                 要语义层
    ④ 归库   resolve                                  有序校验，把文件对到 bgm 条目
    ⑤ 修偏   recheck / split / overflow                三个修正通道
    ⑥ 出库   materialize / nfo                        生成虚拟库 + 集级 NFO
    ⑦ 上线   deploy                                   接进 Jellyfin 并扫描
    ⑧ 验收   report                                   收录情况 + 报告 + 清单

`animediator run` 会按这个顺序一路跑下去，中间产物都在 work/ 里，
断在哪一步都能单独重跑那一步（每步都幂等）。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__


# ── 每个子命令：说明 + 要调的函数（延迟导入，`--help` 不需要装齐东西）──
COMMANDS = {
    "init":       ("在当前目录生成一份 animediator.toml 模板", "animediator.wizard:init_cmd"),
    "config":     ("打印当前生效的配置（排查用）", "animediator.wizard:config_cmd"),
    "doctor":     ("体检：路径、索引、Jellyfin、语义层能否工作", "animediator.wizard:doctor_cmd"),

    "bgm-build":  ("把番组计划 dump.zip 建成离线索引 bgm.db", "animediator.bgm.index:main"),
    "bgm-check":  ("体检：这个索引到底答不答得上真问题", "animediator.bgm.check:main"),
    "bgm-aodb":   ("把外部标题库（aodb）并进作业库，供归库查名字", "animediator.bgm.aodb:main"),
    "bgm-merge":  ("把 aodb 的罗马音/别名并进离线索引", "animediator.bgm.merge:main"),
    "bgm-people": ("把班底/声优/角色解析进离线索引", "animediator.bgm.people:main"),
    "bgm-consts": ("建职位/关联类型的中文对照表（要 PyYAML）", "animediator.bgm.consts:main"),
    "meta":       ("直接查一条元数据（search / get），排查用", "animediator.bgm.meta:main"),

    "scan":       ("扫盘：只读，产出 work/tree.json 与 work/units.json", "animediator.scan:main"),
    "identify":   ("识别：让模型把每个目录通盘整理一遍", "animediator.identify:main"),
    "resolve":    ("归库：有序校验，把文件对到 bgm 条目", "animediator.resolve:main"),
    "recheck":    ("复核：把被误判成特典的正片找回来", "animediator.recheck:main"),
    "split":      ("拆分：一个目录里装了两季的，按模型结论拆开", "animediator.split:main"),
    "overflow":   ("重排：集号溢出（文件比条目容量多）的再判一次", "animediator.overflow:main"),

    "materialize": ("出库：生成虚拟库（符号链接，不动原始文件）", "animediator.materialize:main"),
    "nfo":        ("出库：写集级 NFO（中文集名/简介/声优）", "animediator.nfo:main"),

    "deploy":     ("上线：把虚拟库接进 Jellyfin 并触发扫描", "animediator.deploy:main"),
    "jellyfin":   ("单独操作 Jellyfin：接线/重建/分组/图片/并发/扫描", "animediator.jellyfin:main"),
    "report":     ("验收：收录情况 + HTML 报告 + 清单 csv/md", "animediator.report:main"),

    "reresolve":  ("复核归库 v1：按 unit 让模型看原始路径确认身份", "animediator.reresolve:main"),
    "reresolve-search": ("复核归库 v2：给模型加两轮检索能力", "animediator.reresolve_search:main"),
    "apply":      ("把复核结果写回作业库（带备份/快照，可回滚）", "animediator.apply:main"),
    "translate":  ("给离线库和模型都不知道的条目补中文名", "animediator.translate:main"),

    "run":        ("一键跑完整条链（②→⑧）", "animediator.run:main"),
}


def _global_parent():
    """全局选项的"影子"副本，挂到每个子命令上。

    ⚠️ 为什么要有这个：argparse 默认要求全局选项写在**子命令前面**
      （`animediator --dry-run materialize`）。但人很自然会写成
      `animediator materialize --dry-run`，那样会被当成"unrecognized arguments"。
      把这些选项同时挂到子命令上，两种写法都能用。

      `default=SUPPRESS` 是关键：子命令里没显式给值时**不要**往 namespace 里写，
      否则会把前面已经解析到的全局值覆盖掉。
    """
    p = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS
    p.add_argument("--config", metavar="FILE", default=S)
    p.add_argument("--work-dir", metavar="DIR", default=S)
    p.add_argument("--vfs", metavar="DIR", default=S)
    p.add_argument("--dry-run", action="store_true", default=S)
    p.add_argument("-y", "--yes", action="store_true", default=S)
    p.add_argument("-v", "--verbose", action="store_true", default=S)
    return p


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="animediator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：animediator scan && animediator identify && animediator run",
    )
    p.add_argument("--version", action="version", version="animediator " + __version__)
    p.add_argument("--config", metavar="FILE",
                   help="配置文件路径（默认找 ./animediator.toml）")
    p.add_argument("--work-dir", metavar="DIR", help="中间产物目录（覆盖配置）")
    p.add_argument("--vfs", metavar="DIR", help="虚拟库输出目录（覆盖配置）")
    p.add_argument("--dry-run", action="store_true",
                   help="只报告将要做什么，不写任何东西（会动 Jellyfin 和虚拟库的命令尤其该先用它）")
    p.add_argument("-y", "--yes", action="store_true", help="不交互确认")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", metavar="命令")

    def add(name, help_text, args=()):
        sp = sub.add_parser(name, help=help_text, description=help_text,
                            parents=[_global_parent()])
        for a, kw in args:
            sp.add_argument(*a, **kw)
        return sp

    for name, (help_text, _) in COMMANDS.items():
        add(name, help_text)
        if name == "jellyfin":
            sub.choices[name].add_argument(
                "jf_action", nargs="?",
                choices=["wire", "rebuild", "grouping", "images", "serialize", "scan"],
                help="wire=接线 rebuild=重建库 grouping=分组 images=图片 serialize=并发 scan=扫描")

    # 常用参数单独补全（用 add_parser 再取回来加参数）
    sp = sub.choices["init"]
    sp.add_argument("--detect", action="store_true",
                    help="先探测这台机器（盘位/容器映射/Jellyfin/本地模型）再写模板")

    sp = sub.choices["identify"]
    sp.add_argument("--workers", type=int, help="并发数（默认取配置）")
    sp.add_argument("--only", metavar="正则", help="只处理目录名匹配的单元")
    sp.add_argument("--limit", type=int, help="最多处理多少个单元（先小批试跑）")

    sp = sub.choices["recheck"]
    sp.add_argument("--list", action="store_true", help="只列出待复核的单元，不调模型")
    sp.add_argument("--limit", type=int, help="最多复核多少个单元")
    sp.add_argument("--redo", action="store_true", help="连已判过的也重判")

    sp = sub.choices["split"]
    sp.add_argument("--redo", action="store_true", help="连已拆过的也重拆")
    sp.add_argument("--list", action="store_true", help="只列出待拆的单元，不调模型")
    sp.add_argument("--limit", type=int, help="最多处理多少个")

    for name in ("reresolve", "reresolve-search"):
        sp = sub.choices[name]
        sp.add_argument("--only", metavar="子串", help="只处理单元路径含这个子串的")
        sp.add_argument("--redo", action="store_true",
                        help="只重查上一轮「维持原判」的（new 为空或等于 old）")

    sp = sub.choices["overflow"]
    sp.add_argument("--limit", type=int, help="最多处理多少个组")

    sp = sub.choices["report"]
    sp.add_argument("--no-jellyfin", action="store_true",
                    help="不访问 Jellyfin，只用作业库出报告")

    sp = sub.choices["nfo"]
    sp.add_argument("--write", action="store_true", help="真的写盘（不加就是空跑）")
    sp.add_argument("--series", action="store_true", help="同时写 tvshow.nfo")

    sp = sub.choices["materialize"]
    sp.add_argument("--out", metavar="DIR", help="输出目录（同 --vfs）")

    sp = sub.choices["bgm-build"]
    sp.add_argument("--dump", metavar="ZIP", help="番组计划 dump.zip 路径")
    sp.add_argument("--out", metavar="DB", help="输出 bgm.db 路径")
    sp.add_argument("--check", action="store_true", help="只体检不重建")
    sp.set_defaults(dump=None, out=None, check=False)

    sp = sub.choices["meta"]
    sp.add_argument("argv", nargs="*", help="search <词> 或 get <bgm_id>")

    sp = sub.choices["deploy"]
    sp.add_argument("--rebuild", action="store_true", help="删掉旧库重建（路径大变后必须）")

    sp = sub.choices["run"]
    sp.add_argument("--from", dest="from_step", metavar="步骤", help="从哪一步开始")
    sp.add_argument("--to", dest="to_step", metavar="步骤", help="到哪一步结束")

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return 0

    # ── 装配配置：命令行覆盖 > 环境变量 > 配置文件 > 默认 ──
    from . import config
    overrides = {}
    if args.work_dir:
        overrides["work"] = args.work_dir
    if args.vfs:
        overrides["vfs"] = args.vfs
    cfg = config.load(args.config, overrides)
    cfg.ensure_dirs()
    config.FLAGS.dry_run = args.dry_run
    config.FLAGS.yes = args.yes
    config.FLAGS.verbose = args.verbose

    import importlib
    mod_name, func_name = COMMANDS[args.cmd][1].split(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as ex:
        print("命令 %s 需要的模块没装上：%s" % (args.cmd, ex), file=sys.stderr)
        return 2
    func = getattr(mod, func_name)
    try:
        rc = func(args)
    except KeyboardInterrupt:
        print("\n中断。中间产物在 %s，可以直接重跑这一步。" % cfg.work)
        return 130
    except SystemExit as ex:
        return int(ex.code or 0)
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
