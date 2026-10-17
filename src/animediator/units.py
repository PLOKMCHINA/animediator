"""把扫描结果切成「识别单元」—— 一次 LLM 调用覆盖一个单元。

切分原则（三次踩坑换来的）：
  单元边界 = **发布时本来就分好的那一层**，不是我按文件数拍的阈值。

  ① 按「文件数 / 子目录数」拍阈值 → `TV S1 Naruto` 220 个文件跨 6 个子目录
     被塞进一个单元，模型对不了，只标对前两条，其余整批早期集当成特典。
     → 切太浅。
  ② 手动拼前缀递归 → 路径前缀算错，**丢了 2000 个文件**。
     → 改成先建**显式目录树**再递归，不再手工拼字符串。

  现在的做法：建树 → 递归下降 → 叶子目录（子目录里没有视频）各成一个单元。
  单元名 = 从顶层起的完整路径，既唯一（不会重名丢文件），又自带番名上下文。

  同季集号跨单元的情况，由后面的归库阶段按（番名, 季号）合并，不会丢。
"""
import json

from .config import get as get_cfg

EXCLUDE_TOP = {"Immich", "@eaDir", "#recycle", "lost+found", "Backup",
               "Downloads", "qbittorrent", ".", "System Volume Information"}


def build_tree(dirs):
    """{相对目录: [文件]} → 嵌套树 {段: {子段: {...}, "__files__": [...]}}"""
    root = {}
    for rel, files in dirs.items():
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        node = root
        for p in parts:
            node = node.setdefault(p, {})
        node["__files__"] = list(files)
    return root


def has_videos(node):
    if node.get("__files__"):
        return True
    return any(has_videos(v) for k, v in node.items() if k != "__files__")


def walk(node, prefix, disk, out, stats):
    own = node.get("__files__") or []
    kids = [k for k in sorted(node) if k != "__files__" and has_videos(node[k])]
    label = "/".join(prefix)

    if not kids:
        # 叶子：子目录里没有视频 → 这个目录的文件自成一个单元
        if own:
            out.append({"disk": disk, "top": prefix[0], "name": label,
                        "entries": [(label, own)]})
            stats["units"] += 1
            stats["files"] += len(own)
        return

    # 有更深的子目录：自己直接放的文件单独成单元，然后逐个子目录下探
    if own:
        out.append({"disk": disk, "top": prefix[0], "name": label,
                    "entries": [(label, own)]})
        stats["units"] += 1
        stats["files"] += len(own)
    for k in kids:
        walk(node[k], prefix + [k], disk, out, stats)


def main(args=None):
    cfg = get_cfg()
    d = json.load(open(cfg.tree_json, encoding="utf-8"))
    disks = list(cfg.media.keys()) or list(d.keys())
    all_units = []
    for disk in disks:
        if disk not in d:
            continue
        raw = {r: f for r, f in d[disk]["dirs"].items()
               if r.split("/")[0] not in EXCLUDE_TOP}
        tree = build_tree(raw)
        stats = {"units": 0, "files": 0}
        for top in sorted(tree):
            walk(tree[top], [top], disk, all_units, stats)
        in_files = sum(len(v) for v in raw.values())
        print("%s: 输入 %d 文件 → 切出 %d 单元 / %d 文件 %s" % (
            disk, in_files, stats["units"], stats["files"],
            "✓" if in_files == stats["files"] else "✗ 数量不符!"))

    # 唯一性校验（之前重名导致静默丢文件）
    keys = ["%s|%s" % (u["disk"], u["name"]) for u in all_units]
    if len(keys) != len(set(keys)):
        import collections
        dup = [k for k, c in collections.Counter(keys).items() if c > 1]
        print("!! 单元重名：%s" % dup[:5])
        raise SystemExit(1)
    print("单元唯一性 ✓")

    # 完整性校验：单元里的文件必须与输入完全一致
    src = set()
    for disk in disks:
        for r, fl in d.get(disk, {}).get("dirs", {}).items():
            if r.split("/")[0] in EXCLUDE_TOP:
                continue
            for f in fl:
                src.add((disk, r, f))
    got = set()
    for u in all_units:
        for rel, fl in u["entries"]:
            for f in fl:
                got.add((u["disk"], rel, f))
    if src != got:
        miss = src - got
        print("!! 有 %d 个文件没进任何单元，示例：%s" % (len(miss), list(miss)[:3]))
        raise SystemExit(1)
    print("文件完整性 ✓（每个文件恰好在一个单元里）")

    total_f = sum(len(f) for u in all_units for _, f in u["entries"])
    print("合计 %d 单元 / %d 文件 / 平均 %.1f" % (
        len(all_units), total_f, total_f / len(all_units)))
    for u in sorted(all_units, key=lambda u: -sum(len(f) for _, f in u["entries"]))[:5]:
        print("   最大 %-64s %d 文件" % (u["name"][:64],
                                         sum(len(f) for _, f in u["entries"])))
    with open(cfg.units_json, "w", encoding="utf-8") as f:
        json.dump(all_units, f, ensure_ascii=False, indent=1)
    print("已写入 %s" % cfg.units_json)
    return 0

