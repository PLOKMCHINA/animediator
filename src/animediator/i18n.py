"""界面文案的多语言支持。

设计约束：
- 零第三方依赖，不引入 gettext/Babel。
- 缺失的翻译直接回退到英文，不在运行时抛异常。
- 语言按 配置 > 环境变量 AMI_LANG > LANG/LC_ALL > 默认英文 的顺序解析。

新增文案时在 MESSAGES 里加 key，两个语言都给出。没翻译的语言回退英文。
"""
from __future__ import annotations

import os

DEFAULT_LANG = "en"
SUPPORTED = ("en", "zh")

# 语言在界面上的显示名
LANG_NAMES = {"en": "English", "zh": "简体中文"}

_ALIASES = {
    "zh": "zh", "zh_cn": "zh", "zh-cn": "zh", "zh_hans": "zh",
    "chinese": "zh", "cn": "zh", "中文": "zh",
    "en": "en", "en_us": "en", "en-us": "en", "english": "en", "英文": "en",
}

_current = None


def normalize(lang: str | None) -> str | None:
    """把各种写法归一到受支持的语言代码，不认识返回 None。"""
    if not lang:
        return None
    key = str(lang).strip().lower().replace("-", "_")
    if key in _ALIASES:
        return _ALIASES[key]
    # LANG=zh_CN.UTF-8 这类
    head = key.split(".")[0].split("_")[0]
    return _ALIASES.get(head)


def detect() -> str:
    """按 环境变量 > 系统 locale > 默认 的顺序判断语言。"""
    for var in ("AMI_LANG", "LANG", "LC_ALL", "LC_MESSAGES"):
        got = normalize(os.environ.get(var))
        if got:
            return got
    return DEFAULT_LANG


def set_lang(lang: str | None) -> str:
    """设置当前语言，返回实际生效的值。"""
    global _current
    picked = normalize(lang) or detect()
    _current = picked
    return picked


def current() -> str:
    global _current
    if _current is None:
        _current = detect()
    return _current


def t(key: str, **kw) -> str:
    """取一条文案。找不到 key 时原样返回 key，方便发现漏翻。"""
    lang = current()
    table = MESSAGES.get(lang) or MESSAGES[DEFAULT_LANG]
    text = table.get(key)
    if text is None:
        text = MESSAGES[DEFAULT_LANG].get(key, key)
    try:
        return text.format(**kw) if kw else text
    except (KeyError, IndexError):
        return text


# ── 文案表 ────────────────────────────────────────────────────────────
# 命名约定：<区域>.<用途>，区域与 CLI 分组同名，便于对照查找。

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # 通用
        "common.yes": "yes",
        "common.no": "no",
        "common.none": "(none)",
        "common.continue": "Press Enter to continue",
        "common.cancel": "Cancelled, nothing was written.",
        "common.saved_to": "Written to {path}",
        "common.not_found": "not found",
        "common.exists": "exists",
        "common.missing": "missing",
        "common.done": "Done",

        # 应用
        "app.tagline": "Turn a messy anime folder into a library your media server can read.",
        "app.choose_lang": "Language / 语言",

        # 向导
        "wizard.title": "AniMediator setup",
        "wizard.intro": "A few questions, then a working config. Enter keeps the value shown in brackets.",
        "wizard.step": "Step {n}/{total}: {name}",
        "wizard.detecting": "Scanning this machine...",
        "wizard.detected_disks": "Found {n} mounted volume(s)",
        "wizard.pick_lang": "Interface language",
        "wizard.pick_disks": "Which volumes hold your anime?",
        "wizard.disk_custom": "Enter a path manually",
        "wizard.disk_container": "Container path for {name} (leave empty if not using Docker)",
        "wizard.vfs": "Where should the output library go?",
        "wizard.semantic": "Pick how to read folder names",
        "wizard.semantic_none": "Skip for now (only exact-match steps will run)",
        "wizard.semantic_ollama": "Local Ollama",
        "wizard.semantic_openai": "Any OpenAI-compatible API",
        "wizard.semantic_url": "API base URL",
        "wizard.semantic_model": "Model name",
        "wizard.semantic_key": "API key (leave empty if the server needs none)",
        "wizard.jellyfin": "Do you have Jellyfin running?",
        "wizard.jellyfin_url": "Jellyfin URL",
        "wizard.jellyfin_key": "Jellyfin API key (Console -> API Keys)",
        "wizard.jellyfin_lib": "Existing library name to use",
        "wizard.summary": "About to write:",
        "wizard.confirm": "Write this config?",
        "wizard.written": "Config written to {path}",
        "wizard.next": "Next step: {cmd}",
        "wizard.invalid_path": "That path does not exist. Try again.",
        "wizard.empty": "Please enter a value.",

        # 诊断
        "doctor.title": "Environment check",
        "doctor.blocked": "Blocked",
        "doctor.warnings": "Warnings",
        "doctor.all_good": "No blockers found.",
        "doctor.next": "Next: {cmd}",

        # 错误
        "err.no_config": "No animediator.toml found here. Run `animediator init` first.",
        "err.bad_config": "Could not read {path}: {reason}",
        "err.aborted": "Stopped: {reason}",
    },
    "zh": {
        "common.yes": "是",
        "common.no": "否",
        "common.none": "（无）",
        "common.continue": "按回车继续",
        "common.cancel": "已取消，没有写入任何内容。",
        "common.saved_to": "已写入 {path}",
        "common.not_found": "不存在",
        "common.exists": "存在",
        "common.missing": "缺失",
        "common.done": "完成",

        "app.tagline": "把命名混乱的动漫目录，整理成媒体服务器能直接读取的媒体库。",
        "app.choose_lang": "语言 / Language",

        "wizard.title": "AniMediator 配置向导",
        "wizard.intro": "回答几个问题就能得到一份可用的配置。直接回车沿用括号里的值。",
        "wizard.step": "第 {n}/{total} 步：{name}",
        "wizard.detecting": "正在检查这台机器……",
        "wizard.detected_disks": "找到 {n} 个已挂载的磁盘",
        "wizard.pick_lang": "界面语言",
        "wizard.pick_disks": "哪些盘存放你的动漫？",
        "wizard.disk_custom": "手动输入路径",
        "wizard.disk_container": "{name} 在容器内的路径（不用 Docker 就留空）",
        "wizard.vfs": "输出的媒体库放在哪里？",
        "wizard.semantic": "选择读取目录名的方式",
        "wizard.semantic_none": "暂时跳过（只跑精确匹配的步骤）",
        "wizard.semantic_ollama": "本地 Ollama",
        "wizard.semantic_openai": "任意 OpenAI 兼容接口",
        "wizard.semantic_url": "接口地址",
        "wizard.semantic_model": "模型名",
        "wizard.semantic_key": "API 密钥（服务端不需要就留空）",
        "wizard.jellyfin": "你有在跑的 Jellyfin 吗？",
        "wizard.jellyfin_url": "Jellyfin 地址",
        "wizard.jellyfin_key": "Jellyfin API 密钥（控制台 → API 密钥）",
        "wizard.jellyfin_lib": "要使用的媒体库名",
        "wizard.summary": "即将写入：",
        "wizard.confirm": "确认写入这份配置？",
        "wizard.written": "配置已写入 {path}",
        "wizard.next": "下一步：{cmd}",
        "wizard.invalid_path": "这个路径不存在，请重新输入。",
        "wizard.empty": "请输入内容。",

        "doctor.title": "环境体检",
        "doctor.blocked": "阻塞项",
        "doctor.warnings": "提醒",
        "doctor.all_good": "没发现阻塞项。",
        "doctor.next": "下一步：{cmd}",

        "err.no_config": "当前目录没有 animediator.toml，先跑 `animediator init`。",
        "err.bad_config": "读不了 {path}：{reason}",
        "err.aborted": "已停止：{reason}",
    },
}
