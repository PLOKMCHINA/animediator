"""扫描动漫盘，把视频文件按目录结构摸清楚。

只读，不碰任何文件。输出 work/tree.json 供后续识别层用。

并发说明：扫盘的瓶颈是目录遍历时的元数据读取，等 IO 的时间远多于算。
所以用线程池（不是多进程）把多个一级目录的遍历重叠起来。
机械盘上并发过高会让寻道互相打断，默认值取 4，可用 --workers 调。
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import get as get_cfg
from .textutil import VIDEO_EXT


def _collect(root, start):
    """遍历 start 下的所有目录，只收含视频文件的。"""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(start):
        vids = [f for f in filenames
                if os.path.splitext(f)[1].lower() in VIDEO_EXT]
        if not vids:
            continue
        rel = os.path.relpath(dirpath, root)
        out[rel] = sorted(vids)
    return out


def walk_disk(disk, root, workers=4, on_progress=None):
    """返回 {相对目录: [文件名...]} —— 只收含视频文件的目录。"""
    if workers <= 1:
        return _collect(root, root)

    try:
        entries = list(os.scandir(root))
    except OSError as ex:
        raise SystemExit("读不了 %s：%s" % (root, ex))

    tops, loose = [], []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                tops.append(entry.path)
            elif os.path.splitext(entry.name)[1].lower() in VIDEO_EXT:
                loose.append(entry.name)
        except OSError:
            continue

    result = {}
    if loose:
        result["."] = sorted(loose)

    done = 0
    total = len(tops)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(lambda p: _collect(root, p), tops):
            result.update(part)
            done += 1
            if on_progress and done % 20 == 0:
                on_progress(done, total)
    if on_progress and total:
        on_progress(total, total)
    return result


def main(args=None):
    """扫盘：**只读**，不碰任何文件。盘位来自配置的 [media] 段。

    并行维度是盘，不是目录。同一块机械盘上多线程遍历只会互相打断寻道
    （实测冷缓存下 1 线程 20.9s、8 线程 19.2s，差别在噪声里；热缓存时
    并发反而把 2.9s 拖到 4.5s）。但两块物理盘各读各的，能真正重叠。
    """
    from concurrent.futures import ThreadPoolExecutor

    cfg = get_cfg()
    workers = getattr(args, "workers", None) or cfg.scan_workers

    tasks = []
    for disk, m in cfg.media.items():
        root = (m or {}).get("root", "") if isinstance(m, dict) else (m or "")
        if not root or not os.path.isdir(root):
            print("!! 盘不存在: %s（盘位 %s）" % (root or "(未配)", disk), file=sys.stderr)
            continue
        tasks.append((disk, root))

    if not tasks:
        print("\n一个盘都没扫到 —— 检查配置里的 [media] 段（跑 `animediator doctor`）。")
        return 1

    def do(task):
        disk, root = task
        dirs = walk_disk(disk, root, workers=workers)
        return disk, root, dirs

    result = {}
    if len(tasks) == 1:
        parts = [do(tasks[0])]
    else:
        # 盘之间并行：一块盘一个线程，各自顺序遍历
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            parts = list(pool.map(do, tasks))

    for disk, root, dirs in parts:
        n_files = sum(len(v) for v in dirs.values())
        print("%s: %d 个目录含视频, %d 个视频文件" % (disk, len(dirs), n_files))
        result[disk] = {"root": root, "dirs": dirs}

    cfg.ensure_dirs()
    with open(cfg.tree_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("已写入 %s" % cfg.tree_json)
    return 0
