"""进程退出码。

约定成常量而不是散落的数字，是为了让脚本能判断失败原因：

    0  成功
    1  用法错误（参数写错、命令不存在）
    2  环境缺失（没配置、缺数据、缺依赖服务）
    3  外部服务失败（媒体服务器拒绝、下载失败）
    4  数据问题（索引损坏、结果文件残缺）

所有命令都从 errors 里抛，或者返回这里的常量，不要自己编数字。
"""
from __future__ import annotations

OK = 0
USAGE = 1
ENVIRONMENT = 2
EXTERNAL = 3
DATA = 4

__all__ = ["OK", "USAGE", "ENVIRONMENT", "EXTERNAL", "DATA"]
