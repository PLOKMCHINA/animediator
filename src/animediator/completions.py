"""生成 shell 补全脚本。

    animediator completions bash > ~/.bash_completion.d/animediator
    animediator completions zsh  > ~/.zfunc/_animediator

不做动态补全（不调 Python 进程），静态列出命令和常见选项——
快，而且命令表不常变，重装一次即可。
"""
from __future__ import annotations

from .cli import COMMANDS, GROUPS, TOP_LEVEL

_BASH = r"""# animediator bash 补全
# 安装：animediator completions bash > ~/.bash_completion.d/animediator
_animediator() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local groups="%(groups)s"
    local top="%(top)s"

    # 第一个参数：顶层命令或组名
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$top" -- "$cur") )
        return 0
    fi

    # 第二个参数且第一个是组名：组内命令
    case "${COMP_WORDS[1]}" in
%(group_cases)s
    esac

    # 选项
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "--config --work-dir --vfs --lang --dry-run --yes --verbose --help" -- "$cur") )
        return 0
    fi

    COMPREPLY=( $(compgen -f -- "$cur") )
}
complete -F _animediator animediator
"""

_ZSH = r"""#compdef animediator
# animediator zsh 补全
# 安装：animediator completions zsh > ~/.zfunc/_animediator
_animediator() {
    local -a groups cmds
    groups=(%(groups)s)

    if (( CURRENT == 2 )); then
        _describe 'command' groups
        return
    fi

    case "$words[2]" in
%(group_cases)s
        *) _files ;;
    esac
}
_animediator "$@"
"""


def _group_cases(shell: str) -> str:
    lines = []
    for gname, (_, members) in GROUPS.items():
        joined = " ".join(members)
        if shell == "bash":
            lines.append('        %s) COMPREPLY=( $(compgen -W "%s" -- "$cur") ); return 0 ;;'
                         % (gname, joined))
        else:
            lines.append('        %s) _values "step" %s ;;' % (gname, joined))
    return "\n".join(lines)


def bash() -> str:
    # 顶层命令 + 所有旧命令名，去重后给补全用
    names = list(dict.fromkeys(list(TOP_LEVEL) + list(COMMANDS)))
    return _BASH % {
        "groups": " ".join(GROUPS),
        "top": " ".join(names),
        "group_cases": _group_cases("bash"),
    }


def zsh() -> str:
    top = " ".join(TOP_LEVEL)
    return _ZSH % {
        "groups": " ".join(GROUPS),
        "group_cases": _group_cases("zsh"),
    }


def main(args=None):
    shell = getattr(args, "shell", None) or "bash"
    if shell == "bash":
        print(bash())
    elif shell == "zsh":
        print(zsh())
    else:
        print("只支持 bash 和 zsh。用法：animediator completions bash")
        return 1
    return 0
