"""交互式配置向导。

设计原则：
- 每一问都能回车跳过，默认值来自真实探测，不是拍脑袋的占位符。
- 随时可以 `b` 回上一步、`q` 退出且不写任何文件。
- 非交互环境（管道、CI）自动退回模板生成，不卡在 input()。

向导只负责"问"，把答案整理成配置字段交给 writer，不直接写盘——
这样 `--dry-run` 和测试都能复用同一套问答逻辑。
"""
from __future__ import annotations

import os
import sys

from . import i18n

# ── 交互原语 ──────────────────────────────────────────────────────

class Abort(Exception):
    """用户要求退出。"""


class Back(Exception):
    """用户要求回到上一步。"""


def interactive() -> bool:
    """能不能真的和人对话。"""
    if os.environ.get("AMI_NO_WIZARD"):
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _say(text: str = "") -> None:
    print(text)


def _L(zh: str, en: str) -> str:
    return zh if i18n.current() == "zh" else en


def ask(prompt: str, default: str = "", allow_back: bool = True,
        required: bool = False) -> str:
    """问一句。回车取默认值；b 回退；q 退出。"""
    hint = ""
    if default:
        hint = " [%s]" % default
    if allow_back:
        hint += _L("  (b 返回上一步 / q 退出)", "  (b back / q quit)")
    while True:
        try:
            raw = input("%s%s: " % (prompt, hint)).strip()
        except EOFError:
            raise Abort()
        low = raw.lower()
        if low == "q":
            raise Abort()
        if low == "b" and allow_back:
            raise Back()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            _say(_L("  这一项不能留空。", "  This one cannot be empty."))
            continue
        return raw


def choose(prompt: str, options: list[tuple[str, str]],
           default_index: int = 1, allow_back: bool = True) -> str:
    """单选。options 是 (值, 显示文本)，返回选中的值。"""
    _say()
    _say(prompt)
    for idx, (_, label) in enumerate(options, 1):
        _say("  %d) %s" % (idx, label))
    hint = _L("选一个编号，回车取 %d" % default_index,
              "Pick a number, Enter for %d" % default_index)
    if allow_back:
        hint += _L("，b 返回 / q 退出", ", b back / q quit")
    while True:
        try:
            raw = input("  %s: " % hint).strip()
        except EOFError:
            raise Abort()
        low = raw.lower()
        if low == "q":
            raise Abort()
        if low == "b" and allow_back:
            raise Back()
        if not raw:
            return options[default_index - 1][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        _say(_L("  请输入列表里的编号。", "  Enter one of the numbers listed."))


def multi_choose(prompt: str, options: list[tuple[str, str]],
                 default_keys: list[str] | None = None,
                 allow_back: bool = True) -> list[str]:
    """多选，逗号分隔，如 1,3。"""
    _say()
    _say(prompt)
    for idx, (_, label) in enumerate(options, 1):
        _say("  %d) %s" % (idx, label))
    default_keys = default_keys or []
    default_nums = ",".join(
        str(i) for i, (k, _) in enumerate(options, 1) if k in default_keys)
    hint = _L("编号用逗号分隔", "Comma-separated numbers")
    if default_nums:
        hint += _L("，回车取 %s" % default_nums, ", Enter for %s" % default_nums)
    if allow_back:
        hint += _L("，b 返回 / q 退出", ", b back / q quit")
    while True:
        try:
            raw = input("  %s: " % hint).strip()
        except EOFError:
            raise Abort()
        low = raw.lower()
        if low == "q":
            raise Abort()
        if low == "b" and allow_back:
            raise Back()
        if not raw:
            return default_keys
        picked, bad = [], False
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or not (1 <= int(part) <= len(options)):
                bad = True
                break
            picked.append(options[int(part) - 1][0])
        if bad or not picked:
            _say(_L("  格式不对，重新输。", "  Not a valid pick, try again."))
            continue
        return picked


# ── 各步 ─────────────────────────────────────────────────────────

def step_language(state: dict) -> None:
    _say()
    _say("语言 / Language")
    pick = choose(_L("界面用哪种语言？", "Interface language?"),
                  [("en", "English"), ("zh", "简体中文")],
                  default_index=(2 if i18n.current() == "zh" else 1),
                  allow_back=False)
    i18n.set_lang(pick)
    state["lang"] = pick


def step_disks(state: dict) -> None:
    """挑盘位。探测结果作为候选，也允许手填。"""
    from . import wizard as _w

    _say()
    _say(_L("正在检查这台机器……", "Scanning this machine..."))
    try:
        mounts = _w._mounts()
    except Exception:
        mounts = []
    cmap, _ = _w._docker_media_map()

    cands = [m for m in mounts if m.get("videos", 0) >= 10]
    options = []
    for m in cands[:8]:
        label = "%s  (%s %s)" % (m["root"], m["videos"],
                                 _L("个视频", "videos"))
        container = cmap.get(m["root"])
        if container:
            label += _L("  → 容器内 %s" % container, "  -> in container %s" % container)
        options.append((m["root"], label))
    options.append(("__manual__", _L("手动输入路径", "Enter a path manually")))

    while True:
        if cands:
            picked = multi_choose(
                _L("哪些盘存放你的动漫？", "Which volumes hold your anime?"),
                options, default_keys=[m["root"] for m in cands[:1]])
        else:
            _say(_L("  没探测到明显的媒体盘，手动输入。",
                    "  No obvious media volume found, entering manually."))
            picked = ["__manual__"]

        medias = []
        try:
            for root in picked:
                if root == "__manual__":
                    root = ask(_L("盘上的动漫目录", "Anime folder on disk"),
                               required=True)
                container = cmap.get(root, "")
                if container:
                    answer = ask(
                        _L("%s 在容器内的路径" % root, "Container path for %s" % root),
                        default=container)
                else:
                    answer = ask(
                        _L("%s 在容器内的路径（不用 Docker 就留空）" % root,
                           "Container path for %s (empty if no Docker)" % root),
                        default="")
                medias.append({"root": root, "container": answer or root})
        except Back:
            continue
        state["media"] = medias
        return


def step_vfs(state: dict) -> None:
    default = state.get("vfs", "./vfs")
    state["vfs"] = ask(
        _L("输出的媒体库放在哪里？", "Where should the output library go?"),
        default=default)


def step_semantic(state: dict) -> None:
    from . import wizard as _w

    probe = None
    try:
        probe = _w._probe_models()
    except Exception:
        probe = None

    opts = [("none", _L("暂时跳过（只跑精确匹配，之后可以再开）",
                        "Skip for now (exact matching only, can enable later)"))]
    if probe:
        opts.append(("local", _L("用本机探测到的 %s（%s）"
                                 % (probe["what"], probe["model"]),
                                 "Use the local %s found here (%s)"
                                 % (probe["what"], probe["model"]))))
    opts.append(("openai", _L("任意 OpenAI 兼容接口（云端或自建）",
                              "Any OpenAI-compatible API (cloud or self-hosted)")))

    while True:
        pick = choose(_L("识别目录名用哪种方式？", "How should folder names be read?"),
                      opts, default_index=(2 if probe else 1))
        if pick == "none":
            state["semantic"] = {"backend": "none", "base_url": "",
                                 "model": "", "api_key": ""}
            return
        if pick == "local" and probe:
            state["semantic"] = {"backend": "openai",
                                 "base_url": probe["base_url"],
                                 "model": probe["model"],
                                 "api_key": probe.get("api_key") or "local"}
            return
        try:
            base = ask(_L("接口地址", "API base URL"),
                       default="https://api.openai.com/v1")
            model = ask(_L("模型名", "Model name"), required=True)
            key = ask(_L("API 密钥", "API key"), default="")
        except Back:
            continue
        state["semantic"] = {"backend": "openai", "base_url": base,
                             "model": model, "api_key": key}
        return


def step_jellyfin(state: dict) -> None:
    from . import wizard as _w

    url = None
    try:
        url, info = _w._probe_jellyfin()
    except Exception:
        url, info = None, ""
    if url:
        _say()
        _say(_L("  在这台机器上找到了 Jellyfin：%s" % info,
                "  Found Jellyfin on this machine: %s" % info))

    pick = choose(_L("要现在接上媒体服务器吗？", "Connect a media server now?"),
                  [("yes", _L("是，我有在跑的 Jellyfin", "Yes, I have Jellyfin running")),
                   ("no", _L("先跳过", "Skip for now"))],
                  default_index=(1 if url else 2))
    if pick == "no":
        state["jellyfin"] = {"url": url or "http://127.0.0.1:8096",
                             "api_key": "", "library": "动漫",
                             "vfs_in_container": "/vfs"}
        return

    while True:
        try:
            jf_url = ask(_L("Jellyfin 地址", "Jellyfin URL"),
                         default=url or "http://127.0.0.1:8096")
            key = ask(_L("API 密钥（控制台 → 高级 → API 密钥 新建）",
                         "API key (Console -> Advanced -> API Keys)"), default="")
            lib = ask(_L("要使用的媒体库名", "Library name to use"), default="动漫")
            vfs_in = ask(_L("输出的库在容器内的路径", "Output path inside the container"),
                         default="/vfs")
        except Back:
            continue
        state["jellyfin"] = {"url": jf_url, "api_key": key,
                             "library": lib, "vfs_in_container": vfs_in}
        return


def step_confirm(state: dict) -> bool:
    _say()
    _say("─" * 56)
    _say(_L("即将写入：", "About to write:"))
    _say("  %s: %s" % (_L("语言", "Language"), state.get("lang")))
    for m in state.get("media") or []:
        _say("  %s: %s → %s" % (_L("盘位", "Volume"), m["root"], m["container"]))
    _say("  %s: %s" % (_L("输出目录", "Output"), state.get("vfs")))
    sem = state.get("semantic") or {}
    _say("  %s: %s" % (_L("语义层", "Semantic layer"),
                       sem.get("backend", "none")))
    if sem.get("model"):
        _say("           %s @ %s" % (sem["model"], sem["base_url"]))
    jf = state.get("jellyfin") or {}
    _say("  %s: %s" % ("Jellyfin", jf.get("url")))
    _say("─" * 56)

    pick = choose(_L("确认写入这份配置？", "Write this config?"),
                  [("yes", _L("写", "Write it")),
                   ("no", _L("不了，退出", "No, quit"))], default_index=1)
    return pick == "yes"


# ── 主流程 ───────────────────────────────────────────────────────

STEPS = [
    ("lang", step_language),
    ("disks", step_disks),
    ("vfs", step_vfs),
    ("semantic", step_semantic),
    ("jellyfin", step_jellyfin),
]


def run() -> dict | None:
    """跑完整套问答。用户中途退出返回 None。"""
    state: dict = {}
    idx = 0
    while idx < len(STEPS):
        name, fn = STEPS[idx]
        try:
            fn(state)
            idx += 1
        except Back:
            # 第一问没有上一步，重问自己
            idx = max(0, idx - 1)
        except Abort:
            return None
        except KeyboardInterrupt:
            return None
    if not step_confirm(state):
        return None
    return state
