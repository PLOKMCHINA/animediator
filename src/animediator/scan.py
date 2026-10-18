"""扫描动漫盘，把视频文件按目录结构摸清楚。

只读，不碰任何文件。输出 work/tree.json 供后续识别层用。

并发怎么定的 —— 依据是实测，不是想象：

  * 同一块机械盘上开多线程遍历，只会让磁头来回跑。冷缓存实测 1 线程 20.9 秒、
    8 线程 19.2 秒（差别在噪声里）；热缓存时并发反而把 2.9 秒拖到 4.5 秒。
  * 不同物理盘各读各的，能真正重叠。两块盘实测 16.0 秒降到 12.2 秒。

所以策略是按盘分发、盘内顺序：一块盘一个线程。盘数是几就开几个，单盘就是
单线程（那本来就是最快的方式），八块盘就是八个线程，不需要预先知道有几块盘。

盘内并行只对固态盘开放：机械盘上它是负优化，固态盘上没有寻道这个约束。
盘是不是机械盘从 /sys/dev/block/<major>:<minor>/queue/rotational 读，
读不到（非 Linux、虚拟卷、权限不足）就按机械盘处理 —— 保守的那一边。

--workers 控制的是总并发上限，不是每题盘的线程数。
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import get as get_cfg
from .textutil import VIDEO_EXT

# 盘内并发的上限。固态盘上多开有意义，但不必开太多。
INNER_CAP = 4


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


def is_rotational(path):
    """这块盘是不是机械盘。判断不了就当机械盘 —— 宁可慢，不要互相打断寻道。

    /sys/dev/block/<major>:<minor> 对分区来说指向分区节点，而 queue/ 挂在
    父磁盘下面，所以要往上找。直接读分区节点会读不到，然后静默走保守分支，
    把 NVMe 当成机械盘 —— 这个坑踩过。
    """
    try:
        st = os.stat(path)
        node = os.path.realpath("/sys/dev/block/%d:%d" % (
            os.major(st.st_dev), os.minor(st.st_dev)))
        for _ in range(4):
            q = os.path.join(node, "queue", "rotational")
            if os.path.exists(q):
                with open(q) as f:
                    return f.read().strip() == "1"
            parent = os.path.dirname(node)
            if parent == node:
                break
            node = parent
        return True
    except (OSError, ValueError, AttributeError):
        return True


def walk_disk(disk, root, workers=1, on_progress=None):
    """返回 {相对目录: [文件名...]} —— 只收含视频文件的目录。

    workers=1 时直接顺序走完整棵树；>1 时按一级目录切分并发。
    调用方负责决定给多少 —— 机械盘应该给 1。
    """
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


def _plan(tasks, workers):
    """决定每块盘给几个线程。返回 [(disk, root, inner), ...]。

    盘内并发只给固态盘。机械盘一律 1 —— 多开只会互相打断寻道。
    总线程数不超过 workers，盘多的时候按顺序摊。
    """
    plan = []
    budget = max(1, workers)
    for disk, root in tasks:
        inner = 1
        if not is_rotational(root) and budget > 1:
            inner = min(INNER_CAP, budget)
        plan.append((disk, root, inner))
    return plan


def main(args=None):
    """扫盘：**只读**，不碰任何文件。盘位来自配置的 [media] 段，有几块算几块。"""
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

    plan = _plan(tasks, workers)

    def do(item):
        disk, root, inner = item
        return disk, root, inner, walk_disk(disk, root, workers=inner)

    # 盘之间并行：一块盘一个线程，但总数不超过 workers。
    # 单盘时不进线程池，直接跑。
    if len(plan) == 1:
        parts = [do(plan[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(plan), max(1, workers))) as pool:
            parts = list(pool.map(do, plan))

    result = {}
    for disk, root, inner, dirs in parts:
        n_files = sum(len(v) for v in dirs.values())
        print("%s: %d 个目录含视频, %d 个视频文件（盘内%s）" % (
            disk, len(dirs), n_files,
            "并发 %d" % inner if inner > 1 else "顺序"))
        result[disk] = {"root": root, "dirs": dirs}

    cfg.ensure_dirs()
    with open(cfg.tree_json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("已写入 %s" % cfg.tree_json)
    return 0
