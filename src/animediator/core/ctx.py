"""运行上下文。

一个进程一个 ctx，持有这次调用需要的全部环境：配置、命令行开关、语言、输出器。

为什么不直接改成参数传递：现有 8000 行代码里有 25 个模块调 `config.get()`，
一次性改签名风险太大。这里让 ctx 包住全局状态，新代码走 ctx，老代码不动，
两边看到的是同一份配置。
"""
from __future__ import annotations

from pathlib import Path

from . import codes
from .errors import EnvironmentProblem
from .out import Out


class Context:
    def __init__(self) -> None:
        self.config_path: str | None = None
        self.dry_run: bool = False
        self.assume_yes: bool = False
        self.lang: str = "en"
        self.out: Out = Out()
        self._cfg = None

    # ── 配置 ──────────────────────────────────────────────
    def load_config(self, path: str | None = None, overrides: dict | None = None):
        """装载配置。CLI 只调这一次，其余模块拿同样的对象。"""
        from .. import config as _config
        try:
            cfg = _config.load(path, overrides or {})
        except FileNotFoundError:
            raise EnvironmentProblem(
                "找不到配置文件 animediator.toml",
                hint="先跑 animediator init 生成一份",
                )
        except Exception as ex:
            raise EnvironmentProblem("读取配置失败：%s" % ex,
                                     hint="用 animediator config 看看解析出什么")
        self.config_path = str(getattr(cfg, "path", "") or path or "")
        self._cfg = cfg
        return cfg

    @property
    def cfg(self):
        if self._cfg is None:
            self.load_config(self.config_path)
        return self._cfg

    # ── 语言 ──────────────────────────────────────────────
    def use_lang(self, lang: str | None) -> str:
        from .. import i18n
        self.lang = i18n.set_lang(lang)
        return self.lang

    def t(self, key: str, **kw) -> str:
        from .. import i18n
        return i18n.t(key, **kw)


ctx = Context()
