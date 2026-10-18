"""统一输出。

目的只有一个：让每个命令的提示长得一样，并且错误一定能被脚本看见。

旧代码里各模块自己 print、自己 return 数字，导致同一种失败在不同命令里
文案不同、退出码也不同。新代码走 out.*，老模块可以继续用 print，两边不冲突。
"""
from __future__ import annotations

import sys

from . import codes

_TTY = sys.stdout.isatty()


class Out:
    """往 stdout/stderr 写东西的唯一入口。"""

    def __init__(self, stream=None, err=None, color: bool | None = None):
        self.stream = stream or sys.stdout
        self.err = err or sys.stderr
        self.color = (color if color is not None else _TTY) and not _NO_COLOR

    # ── 基础 ──────────────────────────────────────────────
    def write(self, text: str = "") -> None:
        print(text, file=self.stream)

    def _paint(self, code: str, text: str) -> str:
        if not self.color:
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    # ── 分节与步骤 ─────────────────────────────────────────
    def title(self, text: str) -> None:
        self.write()
        self.write(self._paint("1", text))
        self.write("─" * 60)

    def step(self, n: int, total: int, name: str) -> None:
        self.write()
        self.write(self._paint("1", "[%d/%d] %s" % (n, total, name)))

    # ── 状态行 ────────────────────────────────────────────
    def ok(self, text: str) -> None:
        self.write("  %s %s" % (self._paint("32", "✓"), text))

    def warn(self, text: str) -> None:
        self.write("  %s %s" % (self._paint("33", "!"), text))

    def fail(self, text: str) -> None:
        self.write("  %s %s" % (self._paint("31", "✗"), text))

    def info(self, text: str) -> None:
        self.write("  " + text)

    def error(self, text: str) -> None:
        """错误走 stderr，管道重定向时不会混进正常输出。"""
        print(self._paint("31", text), file=self.err)

    # ── 结构化 ────────────────────────────────────────────
    def table(self, rows, headers=None) -> None:
        """按列宽对齐的简单表格。rows 是等长序列的列表。"""
        rows = [list(map(str, r)) for r in rows]
        if headers:
            rows = [list(map(str, headers))] + rows
        if not rows:
            return
        ncol = max(len(r) for r in rows)
        widths = [0] * ncol
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], len(cell))
        for idx, r in enumerate(rows):
            line = "  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r))
            self.write(self._paint("1", line) if idx == 0 and headers else line)


class _NoColor:
    def __bool__(self):
        return False


_NO_COLOR = bool(__import__("os").environ.get("NO_COLOR"))


out = Out()
