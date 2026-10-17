"""文本归一化 —— 原先是**七份拷贝**散在各模块里的同一个函数。

归一化的目的：让「罗马音标题」和「日文原名」能撞上。
`Sousou no Frieren` / `葬送のフリーレン` / `葬送的芙莉莲` 三个写法里，
检索侧要能互相找到，靠的就是把大小写、标点、全角半角、西里尔形近字
全部折平。

⚠️ 这段映射表不是"随便写的等价类"：西里尔字母里 `А В Е Н К М О Р Т Х У`
和拉丁 `A B E H K M O P T X Y` **长得几乎一样**，而日本动画标题里
混进西里尔字（尤其俄语转写）非常常见，不折平就永远匹配不上。
"""
from __future__ import annotations

import re

_FOLD = {
    "И": "I", "и": "i", "й": "i",
    "А": "A", "а": "a", "В": "B", "в": "b", "С": "C", "с": "c",
    "Е": "E", "е": "e", "Н": "H", "н": "h", "К": "K", "к": "k",
    "М": "M", "м": "m", "О": "O", "о": "o", "Р": "P", "р": "p",
    "Т": "T", "т": "t", "Х": "X", "х": "x", "У": "Y", "у": "y",
    "І": "I", "і": "i", "Ј": "J", "ј": "j",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y",
}

_KEEP = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff]")


def norm(s: str) -> str:
    """小写、只留字母数字与中日文、形近字符折叠。

    ★ 这是**建索引用的**那一个。改动它会让已有的索引和查询对不上，
      所以它必须保持稳定 —— 想改语义请新加函数，别动这里。
    """
    s = "".join(_FOLD.get(c, c) for c in (s or ""))
    s = s.lower().replace("_", " ")
    return _KEEP.sub("", s)


def norm_query(s: str) -> str:
    """查询侧归一化 = `norm()` + 修正**模型输出**在形近字符上的打滑。

    与 `norm()` 的差别只有一条：**整串没有小写字母时**（全大写语境），
    数字 `1` 折成 `I`。

    ⚠️ 为什么不能和 `norm()` 合并：索引里的 norm 是 `norm()` 算的，
      查询侧多一层修正**意味着两边可能算出不同结果** —— 这是**故意的**
      （模型把 `OSHI` 写成 `OSH1` 时，只有修正过才查得中）。
      合并成一个函数就会把索引和查询锁死成同一套语义，
      以后再想修查询侧就得重建整个索引。

    ⚠️ 实测来源：模型输出在形近字符上会打滑（`OSH1`/`OSHl` 冒充 `OSHI`），
      这跟西里尔 `И` 冒充拉丁 `N` 是同一类错误。
      只在全大写语境下修 —— 别的地方 `1` 就是数字、`l` 就是字母。
    """
    s = "".join(_FOLD.get(c, c) for c in (s or ""))
    if s and not any(c.islower() for c in s):
        s = s.replace("1", "I")
    s = s.lower().replace("_", " ")
    return _KEEP.sub("", s)


# ── 视频扩展名：识别层和扫描层必须用同一份，否则"扫到的"和"认得的"对不上 ──
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mov", ".wmv", ".flv",
             ".rmvb", ".iso", ".mpg", ".mpeg", ".webm", ".rm", ".ogm", ".vob"}

# ── 集号：`[01]`、`- 01`、`E01`、`第01话` 各种写法 ──
EP_PATTERNS = [
    re.compile(r"\[(\d{1,3})\]"),
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),
    re.compile(r"(?:^|[\s_\-\.])(?:E|EP|Episode)[\s_\-\.]*(\d{1,3})", re.I),
    re.compile(r"第\s*(\d{1,3})\s*[话話集]"),
]


def guess_episode(filename: str):
    """从文件名里猜集号；猜不到返回 None。

    只做"能直接看出来"的那一类，不做语义判断 ——
    真正的集号归属由归库层和模型决定（谁该判就谁判）。
    """
    stem = filename.rsplit(".", 1)[0]
    m = EP_PATTERNS[1].search(stem)          # SxxExx 最可靠，先试
    if m:
        return int(m.group(2))
    for pat in EP_PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                return int(m.group(m.lastindex or 1))
            except (TypeError, ValueError):
                continue
    return None


def is_video(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[-1].lower() and \
        ("." + name.rsplit(".", 1)[-1].lower()) in VIDEO_EXT
