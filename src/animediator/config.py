"""配置 —— 一份文件说完所有环境相关的事。

════════ 为什么要单独立这一层 ════════
这套工具原来散着 **35 个 `AMI_*` 环境变量 + 20 多处硬编码绝对路径**
（具体盘位的 uuid 路径、固定版本的输出目录、还有作者本机的配置路径）。
那样的东西只能在一台机器上跑，换个机器第一步就卡住。

现在所有环境相关的东西**只在这一个文件里出现**：
路径、盘位、Jellyfin 地址、语义层后端 —— 全部来自配置文件，
代码里不再有任何机器专有的字面量。

════════ 取值优先级（从高到低）════════
  ① 命令行参数      `--config` / `--work-dir` 等
  ② 环境变量        `AMI_*`（保留旧名，方便脚本迁移和 CI）
  ③ 配置文件        animediator.toml（或 .json）
  ④ 内置默认值      相对当前目录，开到哪都能跑

配置文件查找顺序：
  `--config` 指定 → `$AMI_CONFIG` → 当前目录 `animediator.toml`
  → `~/.config/animediator/config.toml`
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── 内置默认值：全部相对路径，保证在任意目录能跑起来 ──
DEFAULTS = {
    "work": "work",
    "data": "data",
    "vfs": "vfs",
    "dump": None,          # 默认 <data>/dump.zip
    "bgm_db": None,        # 默认 <data>/bgm.db
    "media": {},           # SDA/SDB → {root, container}
    "jellyfin": {
        "url": "http://127.0.0.1:8096",
        "api_key": "",
        "library": "动漫",
        "vfs_in_container": "/vfs",
        "user_id": "",
        "library_id": "",      # 库的 ItemId（report 的 ep_enrich/ep_split 用，doctor 可查）
    },
    "semantic": {
        "backend": "none",     # none | openai
        "base_url": "",
        "api_key": "",
        "model": "",
        # 云上推理模型（如 sensenova）不关思考会把 token 全烧在推理里，
        # content 直接是空的。默认不关，只有真遇到才开。
        "disable_thinking": False,
        "workers": 8,
        "timeout": 240,
        "retries": 10,
        "max_tokens": 16000,
        "temperature": 0.1,
    },
    "bgm": {"user_agent": "animediator/1.0 (anime library organizer)"},
    "proxy": "",
}

# 旧环境变量名 → 配置路径。保留是为了让已有脚本/CI 不改也能跑。
_ENV_MAP = {
    "AMI_WORK": "work",
    "AMI_DATA": "data",
    "AMI_VFS": "vfs",
    "AMI_DUMP": "dump",
    "AMI_BGMDb": "bgm_db",
    "AMI_UA": "bgm.user_agent",
    "AMI_PROXY": "proxy",
    "AMI_PROVIDER": "semantic.backend",
    "AMI_URL": "semantic.base_url",
    "AMI_KEY": "semantic.api_key",
    "AMI_MODEL": "semantic.model",
    "AMI_WORKERS": "semantic.workers",
    "AMI_TIMEOUT": "semantic.timeout",
    "AMI_RETRIES": "semantic.retries",
    "JF": "jellyfin.url",
    "JELLYFIN_API_KEY": "jellyfin.api_key",
    "AMI_JF_LIB": "jellyfin.library",
}

# 后端别名：写 hc / huancheng 也认（历史用法）
_BACKEND_ALIAS = {"hc": "openai", "huancheng": "openai", "zhipu": "openai",
                  "": "none", "off": "none", "no": "none"}


class ConfigError(Exception):
    pass


def _deep_set(d: dict, dotted: str, value) -> None:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            return
    cur[parts[-1]] = value


def _load_file(path: Path) -> dict:
    """读配置文件。.toml 用标准库 tomllib（3.11+），.json 用 json。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import tomllib                      # Python 3.11+
    except ModuleNotFoundError:
        raise ConfigError(
            "读取 %s 需要 Python 3.11+（标准库 tomllib）。\n"
            "  你当前是 %s。\n"
            "  两个选择：用 .json 写配置，或者把配置存成 animediator.json。"
            % (path, ".".join(map(str, sys.version_info[:3]))))
    return tomllib.loads(text)


def _find_config(explicit: str | None) -> Path | None:
    cands = []
    if explicit:
        cands.append(Path(explicit))
    elif os.environ.get("AMI_CONFIG"):
        cands.append(Path(os.environ["AMI_CONFIG"]))
    else:
        cands += [
            Path.cwd() / "animediator.toml",
            Path.cwd() / "animediator.json",
            Path.home() / ".config" / "animediator" / "config.toml",
            Path.home() / ".config" / "animediator" / "config.json",
        ]
    for c in cands:
        if c.is_file():
            return c
        if explicit:
            raise ConfigError("配置文件不存在：%s" % c)
    return None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Media(dict):
    """一个盘位。`root` 是宿主真实路径，`container` 是容器里看到的路径。

    两个都要，是因为生成的符号链接要被**容器内的 Jellyfin** 解析 ——
    链接目标必须写成容器视角的路径，而不是宿主的。
    单机直接跑（不经 Docker）时两者填一样即可。
    """

    @property
    def root(self) -> str:
        return self.get("root", "")

    @property
    def container(self) -> str:
        return self.get("container") or self.root


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        base = Path(os.path.abspath(os.path.dirname(str(path)))) if path else Path.cwd()

        def p(key, default_rel):
            v = data.get(key) or default_rel
            q = Path(v)
            return q if q.is_absolute() else (base / q)

        self.work = p("work", DEFAULTS["work"])
        self.data_dir = p("data", DEFAULTS["data"])
        self.vfs = p("vfs", DEFAULTS["vfs"])
        self.dump = Path(data["dump"]) if data.get("dump") else (self.data_dir / "dump.zip")
        self.bgm_db = Path(data["bgm_db"]) if data.get("bgm_db") else (self.data_dir / "bgm.db")

    # ── 中间产物路径：所有模块只从这里取，不再自己拼 ──
    def w(self, name: str) -> Path:
        """work/ 下的一个产物路径。"""
        return self.work / name

    @property
    def db(self) -> Path:
        return self.work / "animediator.db"

    @property
    def tree_json(self) -> Path:
        return self.work / "tree.json"

    @property
    def units_json(self) -> Path:
        return self.work / "units.json"

    @property
    def brain_jsonl(self) -> Path:
        return self.work / "brain_v1.jsonl"

    @property
    def recheck_json(self) -> Path:
        return self.work / "recheck_main.json"

    @property
    def split_json(self) -> Path:
        return self.work / "unit_split.json"

    @property
    def overflow_json(self) -> Path:
        return self.work / "overflow_fix.json"

    @property
    def status_json(self) -> Path:
        return self.work / "status.json"

    # ── 子配置 ──
    @property
    def media(self) -> dict:
        return self.data.get("media") or {}

    @property
    def jellyfin(self) -> dict:
        return self.data.get("jellyfin") or {}

    @property
    def bgm(self) -> dict:
        return self.data.get("bgm") or {}

    @property
    def proxy(self) -> str:
        return self.data.get("proxy") or ""

    @property
    def semantic(self) -> dict:
        s = dict(self.data.get("semantic") or {})
        s["backend"] = _BACKEND_ALIAS.get(str(s.get("backend", "")).lower(),
                                          str(s.get("backend", "")).lower())
        return s

    def ensure_dirs(self) -> None:
        for d in (self.work, self.data_dir):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        cf = str(self.path) if self.path else "（无，全部用默认值）"
        lines = [
            "配置   %s" % cf,
            "工程   work=%s  data=%s" % (self.work, self.data_dir),
            "产物   vfs=%s" % self.vfs,
            "索引   %s" % self.bgm_db,
        ]
        for k, m in sorted(self.media.items()):
            lines.append("盘位   %s  root=%s  container=%s"
                         % (k, m.get("root", "?"), Media(m).container))
        s = self.semantic
        lines.append("语义   backend=%s model=%s workers=%s"
                     % (s.get("backend"), s.get("model") or "-", s.get("workers")))
        lines.append("JF     %s library=%s"
                     % (self.jellyfin.get("url", "-"), self.jellyfin.get("library", "-")))
        return "\n".join(lines)


_CACHE: Config | None = None


def load(config_path: str | None = None, overrides: dict | None = None) -> Config:
    """装配配置。结果会缓存 —— 同一次运行里各处拿到的是同一个对象。"""
    global _CACHE
    path = _find_config(config_path)
    data = _merge(DEFAULTS, _load_file(path) if path else {})

    # ② 环境变量
    for env, dotted in _ENV_MAP.items():
        v = os.environ.get(env)
        if v:
            if dotted in ("semantic.workers", "semantic.timeout", "semantic.retries"):
                v = int(v)
            elif dotted == "semantic.disable_thinking":
                v = v not in ("0", "no", "false", "")
            _deep_set(data, dotted, v)
    if os.environ.get("ZHIPU_API_KEY") and not data["semantic"].get("api_key"):
        data["semantic"]["api_key"] = os.environ["ZHIPU_API_KEY"]
        data["semantic"]["backend"] = "openai"
    if os.environ.get("AMI_NFO_SERIES") or os.environ.get("AMI_NFO_SPECIAL"):
        data.setdefault("nfo", {})
        if os.environ.get("AMI_NFO_SERIES"):
            data["nfo"]["series"] = os.environ["AMI_NFO_SERIES"] not in ("0", "no", "false", "")
        if os.environ.get("AMI_NFO_SPECIAL"):
            data["nfo"]["special"] = os.environ["AMI_NFO_SPECIAL"] not in ("0", "no", "false", "")

    # ① 命令行覆盖（最高）
    for k, v in (overrides or {}).items():
        _deep_set(data, k, v)

    _CACHE = Config(data, path)
    return _CACHE


def get() -> Config:
    """取当前配置；没初始化过就按默认流程装一个。"""
    return _CACHE if _CACHE is not None else load()


# ── 全局开关：一切"要不要写盘/要不要动别人的系统"都从这里问 ──
class Flags:
    """由 CLI 统一设置。`--dry-run` 时任何模块都不许写外部的系统（Jellyfin、
    虚拟库、NFO），只打印将要做什么。

    ★ 这一条是硬要求：这套工具会碰用户的媒体库和 Jellyfin 配置，
      "先看清楚再动手"必须是默认姿势。
    """

    dry_run: bool = False
    yes: bool = False
    verbose: bool = False


FLAGS = Flags()
