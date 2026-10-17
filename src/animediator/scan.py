"""扫描两个动漫盘，把视频文件按目录结构摸清楚。

只读，不碰任何文件。输出 work/tree.json 供后续识别层用。
"""
import json
import os
import sys

from .config import get as get_cfg
from .textutil import VIDEO_EXT


def walk_disk(disk, root):
    """返回 {相对目录: [文件名...]} —— 只收含视频文件的目录。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        vids = [f for f in filenames
                if os.path.splitext(f)[1].lower() in VIDEO_EXT]
        if not vids:
            continue
        rel = os.path.relpath(dirpath, root)
        out[rel] = sorted(vids)
    return out


def main(args=None):
    """扫盘：**只读**，不碰任何文件。盘位来自配置的 [media] 段。"""
    cfg = get_cfg()
    result = {}
    for disk, m in cfg.media.items():
        root = (m or {}).get("root", "") if isinstance(m, dict) else (m or "")
        if not root or not os.path.isdir(root):
            print("!! 盘不存在: %s（盘位 %s）" % (root or "(未配)", disk), file=sys.stderr)
            continue
        dirs = walk_disk(disk, root)
        n_files = sum(len(v) for v in dirs.values())
        print("%s: %d 个目录含视频, %d 个视频文件" % (disk, len(dirs), n_files))
        result[disk] = {"root": root, "dirs": dirs}

    if not result:
        print("\n一个盘都没扫到 —— 检查配置里的 [media] 段（跑 `animediator doctor`）。")
        return 1
    cfg.ensure_dirs()
    with open(cfg.tree_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("已写入 %s" % cfg.tree_json)
    return 0

