"""检查虚拟库和源文件是否还对得上。

虚拟库整个是由符号链接搭起来的，源文件一旦动了（删了、改名了、坏道读不出来），
链接就变成死链 —— 表现是 Jellyfin 里能看见条目，点开播不了，而且 Jellyfin 自己
不一定报错。这里把死链找出来，只读，不修。

链接目标写的是**容器里的路径**（materialize 按容器视角写，见那边的说明），
所以在宿主上验存在性要先把容器路径映射回宿主路径，否则每一条都会被判成死的。

    animediator lib verify            检查，有死链时退出码 1
    animediator lib verify --sample 50  多列几条

退出码：0 全部正常；1 有死链；2 虚拟库不存在（还没跑过 materialize）。
"""
from __future__ import annotations

import os
import sys

from .config import get as get_cfg


def host_path(target, cfg):
    """把容器内路径映射回宿主路径。映射不上就原样返回。"""
    t = target.rstrip("/")
    for _disk, m in (cfg.media or {}).items():
        if not isinstance(m, dict):
            continue
        c = (m.get("container") or "").rstrip("/")
        if not c:
            continue
        if t == c:
            return (m.get("root") or "").rstrip("/") or t
        if t.startswith(c + "/"):
            rest = t[len(c) + 1:]
            root = (m.get("root") or "").rstrip("/")
            return os.path.join(root, rest) if root else t
    return t


def _walk_links(vfs):
    """列出虚拟库里所有符号链接和它们的原始目标。"""
    for dirpath, dirnames, filenames in os.walk(vfs):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                try:
                    yield p, os.readlink(p)
                except OSError:
                    yield p, ""


def main(args=None):
    cfg = get_cfg()
    vfs = cfg.vfs
    if not vfs.exists():
        print("虚拟库还没生成：%s" % vfs)
        print("先跑 animediator lib materialize。")
        return 2

    sample = getattr(args, "sample", None) or 20
    total = 0
    dead = []
    relative = 0
    for link, target in _walk_links(vfs):
        total += 1
        if not target:
            dead.append((link, "(读不出来)"))
            continue
        if os.path.isabs(target):
            real = host_path(target, cfg)
        else:
            # 符号链接按自身所在目录解析，不是按虚拟库根。写成相对的几乎都不是
            # 故意的 —— 盘位名和配置对不上时 materialize 才会算出相对路径。
            relative += 1
            real = os.path.normpath(os.path.join(os.path.dirname(link), target))
        if not os.path.exists(real):
            dead.append((link, real))

    print("虚拟库 %s" % vfs)
    print("链接 %d 条，其中有效 %d、断链 %d" % (total, total - len(dead), len(dead)))
    if relative:
        print("其中 %d 条写成了相对路径 —— 正常应当是绝对路径" % relative)

    if dead:
        print("\n断链（源文件不在了）：")
        for link, real in dead[:sample]:
            print("  %s" % os.path.relpath(link, vfs))
            print("      → %s" % real)
        if len(dead) > sample:
            print("  …… 还有 %d 条（--sample 调大可以多看几条）" % (len(dead) - sample))
        print("\n这些条目在媒体库里还在，点开会播不了。")
        if relative == total:
            print("全部都是相对路径，多半是 [media] 的段名和 unit 表记的盘位名对不上。")
            print("对一下 `animediator lib assign --list` 里的来源和配置里的盘位名。")
        else:
            print("确认源文件是不是被移走了；确认不回来就重跑 materialize"
                  "（会按当前库重新生成链接）。")
        return 1

    # 顺手看一眼有没有盘位配了但目录不存在 —— 死链之外最常见的配置错
    missing_root = []
    for disk, m in (cfg.media or {}).items():
        root = (m or {}).get("root", "") if isinstance(m, dict) else (m or "")
        if root and not os.path.isdir(root):
            missing_root.append((disk, root))
    if missing_root:
        print("\n配了但宿上找不到的盘位：")
        for disk, root in missing_root:
            print("  %s → %s" % (disk, root))
        return 1

    print("没发现问题。")
    return 0
