"""命令行入口。

    animediator <命令> [选项]

命令分五组。第一次用只需要两条：`init` 生成配置，`run` 跑完整条链。

    setup   首次配置             init / config / doctor
    lib     整理媒体库           scan / identify / resolve / materialize / nfo / ...
    index   维护离线索引         bgm-build / bgm-update / meta / ...
    serve   接入服务器与报告     deploy / jellyfin / report / translate
    run     一条命令跑完全程

所有旧的一级命令名继续可用（`animediator scan` 等价于 `animediator lib scan`），
现有脚本和文档不受影响。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .core import AmiError, codes, ctx

# ── 命令表：说明 + 实现位置 ────────────────────────────────────────
COMMANDS = {
    "init":       ("在当前目录生成配置（带引导问答）", "animediator.wizard:init_cmd"),
    "config":     ("打印当前生效的配置（排查用）", "animediator.wizard:config_cmd"),
    "doctor":     ("体检：路径、索引、媒体服务器、语义层能否工作", "animediator.wizard:doctor_cmd"),
    "status":     ("进度：这批数据走到哪一步了，下一步跑什么", "animediator.status:main"),
    "completions": ("生成 shell 补全脚本（bash / zsh）", "animediator.completions:main"),

    "bgm-build":  ("建立离线索引（自动获取最新数据，无需手工下载）", "animediator.bgm.index:main"),
    "bgm-update": ("只检查并更新离线数据，不重建索引", "animediator.bgm_index_update:main"),
    "bgm-check":  ("体检：这个索引到底答不答得上真问题", "animediator.bgm.check:main"),
    "bgm-aodb":   ("把外部标题库（aodb）并进作业库，供归库查名字", "animediator.bgm.aodb:main"),
    "bgm-merge":  ("把 aodb 的罗马音/别名并进离线索引", "animediator.bgm.merge:main"),
    "bgm-people": ("把班底/声优/角色解析进离线索引", "animediator.bgm.people:main"),
    "bgm-consts": ("建职位/关联类型的中文对照表（要 PyYAML）", "animediator.bgm.consts:main"),
    "meta":       ("直接查一条元数据（search / get），排查用", "animediator.bgm.meta:main"),

    "scan":       ("扫盘：只读，产出 work/tree.json 与 work/units.json", "animediator.scan:main"),
    "identify":   ("识别：让模型把每个目录通盘整理一遍", "animediator.identify:main"),
    "resolve":    ("归库：有序校验，把文件对到 bgm 条目", "animediator.resolve:main"),
    "rulematch":  ("降级匹配：不调模型，只用目录名查索引", "animediator.rulematch:main"),
    "assign":     ("人工指定：某个目录对应哪部作品", "animediator.assign:main"),
    "undo":       ("回滚：从备份还原条目归属或整库", "animediator.undo:main"),
    "verify":     ("体检：虚拟库的链接是否还指向存在的源文件", "animediator.verify:main"),
    "localize":   ("翻译：把非中文简介翻成中文并缓存", "animediator.localize:main"),
    "recheck":    ("复核：把被误判成特典的正片找回来", "animediator.recheck:main"),
    "split":      ("拆分：一个目录里装了两季的，按模型结论拆开", "animediator.split:main"),
    "overflow":   ("重排：集号溢出的再判一次", "animediator.overflow:main"),

    "materialize": ("出库：生成虚拟库（符号链接，不动原始文件）", "animediator.materialize:main"),
    "nfo":        ("出库：写集级 NFO（中文集名/简介/声优）", "animediator.nfo:main"),

    "deploy":     ("上线：把虚拟库接进媒体服务器并触发扫描", "animediator.deploy:main"),
    "jellyfin":   ("单独操作 Jellyfin：接线/重建/分组/图片/并发/扫描", "animediator.jellyfin:main"),
    "report":     ("验收：收录情况 + HTML 报告 + 清单 csv/md", "animediator.report:main"),

    "reresolve":  ("复核归库 v1：按 unit 让模型看原始路径确认身份", "animediator.reresolve:main"),
    "reresolve-search": ("复核归库 v2：给模型加两轮检索能力", "animediator.reresolve_search:main"),
    "apply":      ("把复核结果写回作业库（带备份/快照，可回滚）", "animediator.apply:main"),
    "translate":  ("给离线库和模型都不知道的条目补中文名", "animediator.translate:main"),
    "run":        ("一键跑完整条链", "animediator.run:main"),
}

# 分组：名字 -> (一句话说明, 成员命令)
GROUPS = {
    "setup": ("首次配置与自检", ["init", "config", "doctor", "status", "completions"]),
    "lib": ("整理媒体库：扫描、认片、归库、出库",
            ["scan", "identify", "resolve", "rulematch", "assign", "undo", "verify", "localize", "recheck", "split", "overflow",
             "reresolve", "reresolve-search", "apply",
             "materialize", "nfo"]),
    "index": ("维护离线索引（番组计划数据）",
              ["bgm-build", "bgm-update", "bgm-check", "bgm-aodb",
               "bgm-merge", "bgm-people", "bgm-consts", "meta"]),
    "serve": ("接入媒体服务器与出报告",
              ["deploy", "jellyfin", "report", "translate"]),
}

# 顶层帮助里显示的命令（其余仍然可用，只是不占据视线）
TOP_LEVEL = ["init", "run", "doctor", "status"] + list(GROUPS)


def _global_parent():
    """全局选项的影子副本，挂到每个子命令上。

    argparse 默认要求全局选项写在子命令前面，但人自然会写在后面
    （`animediator materialize --dry-run`）。同时挂一份，两种写法都能用。

    default=SUPPRESS 是关键：子命令没给值时不要往 namespace 写，
    否则会覆盖前面已解析到的值。
    """
    p = argparse.ArgumentParser(add_help=False)
    S = argparse.SUPPRESS
    p.add_argument("--config", metavar="FILE", default=S)
    p.add_argument("--work-dir", metavar="DIR", default=S)
    p.add_argument("--vfs", metavar="DIR", default=S)
    p.add_argument("--dry-run", action="store_true", default=S)
    p.add_argument("--lang", metavar="LANG", default=S)
    p.add_argument("-y", "--yes", action="store_true", default=S)
    p.add_argument("-v", "--verbose", action="store_true", default=S)
    return p


def _add_command_args(sp, name: str) -> None:
    """给某个子命令挂它的专属参数。"""
    if name == "jellyfin":
        sp.add_argument(
            "jf_action", nargs="?",
            choices=["wire", "rebuild", "grouping", "images", "serialize", "scan"],
            help="wire=接线 rebuild=重建库 grouping=分组 images=图片 serialize=并发 scan=扫描")

    elif name == "init":
        sp.add_argument("--detect", action="store_true",
                        help="先探测这台机器（盘位/容器映射/媒体服务器/本地模型）再写配置")
        sp.add_argument("--force", action="store_true", help="已存在配置时覆盖它")
        sp.add_argument("--no-wizard", dest="no_wizard", action="store_true",
                        help="不提问，直接生成模板（可脚本化）")

    elif name == "identify":
        sp.add_argument("--workers", type=int, help="并发数（默认取配置）")
        sp.add_argument("--only", metavar="正则", help="只处理目录名匹配的单元")
        sp.add_argument("--limit", type=int, help="最多处理多少个单元（先小批试跑）")

    elif name == "scan":
        sp.add_argument("--workers", type=int,
                        help="扫盘并发数（默认 4；机械盘调高反而可能变慢）")

    elif name == "assign":
        sp.add_argument("--list", action="store_true", help="列出所有单元现在的归属")
        sp.add_argument("--unmatched", action="store_true", help="配合 --list，只看没认出来的")
        sp.add_argument("--unit", metavar="子串", help="目录名包含这个子串的单元")
        sp.add_argument("--search", metavar="关键词", help="在索引里搜候选，不写库")
        sp.add_argument("--bgm", type=int, metavar="ID", help="指定 bgm 条目 id")
        sp.add_argument("--all", action="store_true", help="命中多个单元时全部应用")

    elif name == "undo":
        sp.add_argument("--list", action="store_true", help="列出可用的备份与快照")
        sp.add_argument("--snapshot", nargs="?", const=True, metavar="文件",
                        help="整库回滚；不给值或配 --latest 用最近一份")
        sp.add_argument("--latest", action="store_true", help="用最近一份快照")
        sp.add_argument("--bgm", action="store_true", help="只还原 bgm_id 一列")
        sp.add_argument("--index", type=int, help="配合 --bgm，用第几份备份（0 = 最近）")
        sp.add_argument("target", nargs="?", help="快照文件名（可写片段）")

    elif name == "verify":
        sp.add_argument("--sample", type=int, metavar="N", help="断链最多列几条，默认 20")

    elif name == "localize":
        sp.add_argument("--stats", action="store_true", help="只统计简介语言，不调模型")
        sp.add_argument("--force", action="store_true", help="已有缓存也重翻")
        sp.add_argument("--limit", type=int, metavar="N", help="这次最多翻几部，试跑用")

    elif name == "completions":
        sp.add_argument("shell", nargs="?", choices=["bash", "zsh"], default="bash",
                        help="给哪个 shell 生成补全（默认 bash）")

    elif name == "recheck":
        sp.add_argument("--list", action="store_true", help="只列出待复核的单元，不调模型")
        sp.add_argument("--limit", type=int, help="最多复核多少个单元")
        sp.add_argument("--redo", action="store_true", help="连已判过的也重判")

    elif name == "split":
        sp.add_argument("--redo", action="store_true", help="连已拆过的也重拆")
        sp.add_argument("--list", action="store_true", help="只列出待拆的单元，不调模型")
        sp.add_argument("--limit", type=int, help="最多处理多少个")

    elif name in ("reresolve", "reresolve-search"):
        sp.add_argument("--only", metavar="子串", help="只处理单元路径含这个子串的")
        sp.add_argument("--redo", action="store_true",
                        help="只重查上一轮「维持原判」的（new 为空或等于 old）")

    elif name == "overflow":
        sp.add_argument("--limit", type=int, help="最多处理多少个组")

    elif name == "report":
        sp.add_argument("--no-jellyfin", action="store_true",
                        help="不访问媒体服务器，只用作业库出报告")

    elif name == "nfo":
        sp.add_argument("--write", action="store_true", help="真的写盘（不加就是空跑）")
        sp.add_argument("--series", action="store_true", help="同时写 tvshow.nfo")

    elif name == "materialize":
        sp.add_argument("--out", metavar="DIR", help="输出目录（同 --vfs）")
        sp.add_argument("--force", action="store_true",
                        help="目标目录非空且看不出是本工具生成的库时，仍然写入")

    elif name == "translate":
        sp.add_argument("--limit", type=int, help="最多处理多少个条目（先小批试跑）")

    elif name == "bgm-consts":
        sp.add_argument("--staffs", metavar="FILE", help="职位对照表路径")
        sp.add_argument("--relations", metavar="FILE", help="关联类型对照表路径")

    elif name == "bgm-build":
        sp.add_argument("--dump", metavar="ZIP", help="手动指定数据文件（指定后不联网）")
        sp.add_argument("--out", metavar="DB", help="输出 bgm.db 路径")
        sp.add_argument("--check", action="store_true", help="只体检不重建")
        sp.add_argument("--offline", action="store_true", help="不联网，只用本地已有的数据")
        sp.add_argument("--force", action="store_true", help="无视版本判断，强制重新下载")
        sp.set_defaults(dump=None, out=None, check=False, offline=False, force=False)

    elif name == "bgm-update":
        sp.add_argument("--offline", action="store_true", help="不联网，只看本地状态")
        sp.add_argument("--force", action="store_true", help="无视版本判断，强制重新下载")
        sp.set_defaults(offline=False, force=False)

    elif name == "meta":
        sp.add_argument("argv", nargs="*", help="search <词> 或 get <bgm_id>")

    elif name == "deploy":
        sp.add_argument("--rebuild", action="store_true",
                        help="删掉旧库重建（路径大变后必须）")
        sp.add_argument("--recreate-container", dest="recreate_container",
                        action="store_true",
                        help="允许删掉现有容器重建（默认只在已挂载时跳过）")

    elif name == "run":
        sp.add_argument("--from", dest="from_step", metavar="步骤", help="从哪一步开始")
        sp.add_argument("--to", dest="to_step", metavar="步骤", help="到哪一步结束")
        sp.add_argument("--no-deploy", action="store_true",
                        help="不接入媒体服务器，只生成文件")
        sp.add_argument("--no-semantic", action="store_true",
                        help="跳过需要语言模型的步骤（用已有的判定结果继续）")
        sp.set_defaults(from_step=None, to_step=None,
                        no_deploy=False, no_semantic=False)


def _overview(parser) -> str:
    """顶层帮助：只列该知道的，不把 27 个命令一次倒出来。"""
    lines = [parser.description or "", "", "常用：", ""]
    lines.append("    animediator init            首次配置（会问你几个问题）")
    lines.append("    animediator run             一条命令跑完全程")
    lines.append("    animediator doctor          检查环境")
    lines.append("")
    lines.append("按用途分组（animediator <组> --help 看组内命令）：")
    lines.append("")
    for gname, (gtitle, members) in GROUPS.items():
        lines.append("    %-8s %-28s %d 个命令" % (gname, gtitle, len(members)))
    lines.append("")
    lines.append("兼容：旧的一级命令名仍然可用，例如 animediator scan。")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="animediator",
        description="把命名混乱的动漫目录，整理成媒体服务器能直接读取的媒体库。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="animediator " + __version__)
    p.add_argument("--config", metavar="FILE", help="配置文件路径（默认找 ./animediator.toml）")
    p.add_argument("--work-dir", metavar="DIR", help="中间产物目录（覆盖配置）")
    p.add_argument("--vfs", metavar="DIR", help="虚拟库输出目录（覆盖配置）")
    p.add_argument("--lang", metavar="LANG", choices=["en", "zh"],
                   help="界面语言（默认跟随系统，也可写进配置）")
    p.add_argument("--dry-run", action="store_true",
                   help="只报告将要做什么，不写任何东西")
    p.add_argument("-y", "--yes", action="store_true", help="不交互确认")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", metavar="命令")

    # 顶层直接可见的三个
    for name in TOP_LEVEL:
        if name in GROUPS:
            continue
        sp = sub.add_parser(name, help=COMMANDS[name][0], description=COMMANDS[name][0],
                            parents=[_global_parent()])
        _add_command_args(sp, name)

    # 分组
    for gname, (gtitle, members) in GROUPS.items():
        gp = sub.add_parser(gname, help=gtitle, description=gtitle.capitalize(),
                            parents=[_global_parent()])
        gsub = gp.add_subparsers(dest="subcmd", metavar="步骤")
        for cname in members:
            sp = gsub.add_parser(cname, help=COMMANDS[cname][0],
                                 description=COMMANDS[cname][0],
                                 parents=[_global_parent()])
            _add_command_args(sp, cname)

    # 旧的一级命令名不注册到解析器里，改为在 main() 入口重写成「组 步骤」。
    # 注册成隐藏命令不行：argparse 会把 help=SUPPRESS 原样打出来。

    return p


# 旧命令名 -> 所属组
ALIASES = {name: gname for gname, (_, members) in GROUPS.items() for name in members}


_VALUE_OPTS = {"--config", "--work-dir", "--vfs", "--lang"}


def _rewrite_alias(argv: list[str]) -> list[str]:
    """`animediator scan` -> `animediator lib scan`，让旧写法继续可用。

    要把全局选项跳过去：`--config x scan` 里的 scan 才是命令，
    简单看 argv[0] 会把 --config 当成命令名的位置。
    """
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _VALUE_OPTS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        if token in ALIASES:
            return argv[:i] + [ALIASES[token], token] + argv[i + 1:]
        return argv
    return argv


def _resolve(args) -> str | None:
    """把 `组 步骤` 或旧命令名归一成一个真实命令名。"""
    if getattr(args, "subcmd", None):
        return args.subcmd
    if args.cmd in COMMANDS:
        return args.cmd
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _rewrite_alias(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        print(_overview(parser))
        return codes.OK

    # 只写了组名、没写步骤：打印这一组的命令清单
    if args.cmd in GROUPS and not getattr(args, "subcmd", None):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and args.cmd in action.choices:
                action.choices[args.cmd].print_help()
                return codes.OK
        return codes.OK

    command = _resolve(args)
    if command is None:
        print("未知命令：%s" % args.cmd, file=sys.stderr)
        print("跑 animediator --help 看全部命令。", file=sys.stderr)
        return codes.USAGE

    # 装配上下文：命令行 > 配置文件 > 环境变量 > 默认
    from . import config
    overrides = {}
    if getattr(args, "work_dir", None):
        overrides["work"] = args.work_dir
    if getattr(args, "vfs", None):
        overrides["vfs"] = args.vfs

    try:
        cfg = config.load(getattr(args, "config", None), overrides)
        cfg.ensure_dirs()
    except AmiError as ex:
        ctx.out.error(ex.render())
        return ex.code

    # 语言要等配置读出来才能定：命令行 --lang 优先，其次配置文件里的 lang
    lang = getattr(args, "lang", None) or (cfg.data or {}).get("lang")
    ctx.use_lang(lang)

    config.FLAGS.dry_run = bool(getattr(args, "dry_run", False))
    config.FLAGS.yes = bool(getattr(args, "yes", False))
    config.FLAGS.verbose = bool(getattr(args, "verbose", False))
    ctx.dry_run = config.FLAGS.dry_run
    ctx.assume_yes = config.FLAGS.yes
    ctx._cfg = cfg
    if not getattr(cfg, "lang", None):
        try:
            cfg.data.setdefault("lang", ctx.lang)
        except Exception:
            pass

    # 让下游模块仍能按命令名分支
    args.cmd = command

    import importlib
    mod_name, func_name = COMMANDS[command][1].split(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as ex:
        ctx.out.error("命令 %s 需要的模块没装上：%s" % (command, ex))
        return codes.ENVIRONMENT
    func = getattr(mod, func_name)

    try:
        rc = func(args)
    except KeyboardInterrupt:
        print("\n中断。中间产物在 %s，可以直接重跑这一步。" % cfg.work)
        return 130
    except AmiError as ex:
        ctx.out.error(ex.render())
        return ex.code
    except SystemExit as ex:
        # 模块里用 `raise SystemExit("说明")` 时，用户要看到那句话而不是裸退出码
        code = ex.code
        if isinstance(code, str):
            print(code)
            return codes.USAGE
        return int(code or 0)
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
