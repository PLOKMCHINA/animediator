"""错误类型。

用途是让「失败」带上退出码和可执行建议，而不是靠调用方猜。

    raise AmiError("缺少配置文件", hint="先跑 animediator init", code=ENVIRONMENT)

CLI 顶层捕到 AmiError 就打印 message + hint 并返回 code，不再打异常栈。
"""
from __future__ import annotations

from . import codes


class AmiError(Exception):
    """带退出码和修复建议的失败。"""

    def __init__(self, message: str, hint: str = "", code: int = codes.ENVIRONMENT):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def render(self) -> str:
        text = self.message
        if self.hint:
            text += "\n  " + self.hint
        return text


class UsageError(AmiError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, hint, codes.USAGE)


class EnvironmentProblem(AmiError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, hint, codes.ENVIRONMENT)


class ExternalFailure(AmiError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, hint, codes.EXTERNAL)


class DataProblem(AmiError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, hint, codes.DATA)


__all__ = [
    "AmiError", "UsageError", "EnvironmentProblem",
    "ExternalFailure", "DataProblem", "codes",
]
