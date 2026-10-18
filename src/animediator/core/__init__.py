"""公共基础设施：退出码、输出器、错误类型、运行上下文。

从旧模块引用时保持两种风格都能工作：

    from animediator.core import ctx, out, codes
    from animediator.core.errors import EnvironmentProblem
"""
from . import codes
from .ctx import Context, ctx
from .errors import (AmiError, DataProblem, EnvironmentProblem,
                     ExternalFailure, UsageError)
from .out import Out, out

__all__ = [
    "codes", "ctx", "Context", "out", "Out",
    "AmiError", "UsageError", "EnvironmentProblem",
    "ExternalFailure", "DataProblem",
]
