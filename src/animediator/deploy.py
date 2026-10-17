"""上线 —— 薄薄一层串联，只调 animediator.jellyfin 里的函数。

    deploy(args)   =  换挂载（重建容器）→ wire / rebuild → grouping → images → scan

真正的逻辑全在 jellyfin.py（六个旧脚本原样合并），这里不持有任何判断。
`--rebuild` 时走 rebuild（删库重建，破坏性，dry-run 会停下），
否则走 wire（只改现有库的选项）。
"""
from __future__ import annotations

from .jellyfin import deploy as _deploy


def main(args=None):
    return _deploy(args)
