"""冒烟测试 —— 不碰真实媒体库、不调模型、不需要网络。

两种跑法都行：
    python tests/test_smoke.py     # 裸 Python，零依赖
    python -m pytest tests/ -q     # 装了 pytest 也能收集（都是普通 assert）

覆盖的是「装了能不能用」的底线：模块能导入、配置能装配、
归一化语义没搞混、安全闸真的会拦。
真正的行为等价性靠 `tools/` 里那两个对比脚本 + 端到端跑一遍，不在单元测试里做。
"""
import contextlib
import os
import sys
import tempfile
import textwrap
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

MODULES = [
    "animediator", "animediator.cli", "animediator.config",
    "animediator.providers", "animediator.textutil", "animediator.db",
    "animediator.wizard", "animediator.run",
    "animediator.scan", "animediator.units", "animediator.identify",
    "animediator.resolve", "animediator.reresolve",
    "animediator.reresolve_search", "animediator.apply",
    "animediator.recheck", "animediator.split", "animediator.overflow",
    "animediator.materialize", "animediator.nfo",
    "animediator.jellyfin", "animediator.deploy", "animediator.report",
    "animediator.translate",
    "animediator.bgm", "animediator.bgm.index", "animediator.bgm.aodb",
    "animediator.bgm.check", "animediator.bgm.merge",
    "animediator.bgm.people", "animediator.bgm.consts", "animediator.bgm.meta",
]


@contextlib.contextmanager
def raises(exc):
    """pytest.raises 的最小替代 —— 不为一个断言把框架变成硬依赖。"""
    try:
        yield
    except exc:
        return
    except Exception as other:
        raise AssertionError("期望 %s，实际抛了 %s: %s"
                             % (exc.__name__, type(other).__name__, other))
    raise AssertionError("期望抛 %s，但什么都没抛" % exc.__name__)


def _write_cfg(tmpdir):
    p = os.path.join(tmpdir, "animediator.toml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            work = "%s/work"
            data = "%s/data"
            vfs  = "%s/vfs"

            [media.DISK1]
            root      = "/tmp"
            container = "/media/disk1"

            [jellyfin]
            url     = "http://127.0.0.1:8096"
            api_key = ""

            [semantic]
            backend = "none"
            """) % (tmpdir, tmpdir, tmpdir))
    return p


# ══════════════════ 测试 ══════════════════

def test_all_modules_import():
    for name in MODULES:
        __import__(name)


def test_no_third_party_runtime_deps():
    """引擎本体必须零第三方依赖 —— 这条是硬约束，不是偏好。

    语义层走标准库 urllib，配置走 tomllib/json。
    唯一允许的第三方是 PyYAML，且必须是**可选**（只给 bgm-consts 读外部对照表）。
    """
    import importlib.util
    src_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    banned = ["requests", "httpx", "pydantic", "click", "aiohttp", "rich"]
    for dirpath, _, files in os.walk(src_root):
        for f in files:
            if not f.endswith(".py"):
                continue
            text = open(os.path.join(dirpath, f), encoding="utf-8").read()
            for mod in banned:
                assert "import %s" % mod not in text, \
                    "%s 引入了第三方依赖 %s（引擎必须零依赖）" % (f, mod)
            if "import yaml" in text:
                assert "try:" in text, "%s 里的 yaml 必须包在 try 里（可选项）" % f


def test_config_loads_and_overrides():
    from animediator import config
    with tempfile.TemporaryDirectory() as d:
        p = _write_cfg(d)
        cfg = config.load(p)
        assert str(cfg.work).endswith("work"), cfg.work
        assert cfg.media["DISK1"]["container"] == "/media/disk1"
        assert cfg.semantic["backend"] == "none"
        # 命令行覆盖优先级最高
        cfg2 = config.load(p, {"vfs": "/tmp/override-vfs"})
        assert str(cfg2.vfs) == "/tmp/override-vfs", cfg2.vfs


def test_two_norms_are_distinct():
    """索引侧和查询侧的归一化**故意不同**，不能合并。

    查询侧多一条：全大写语境下把数字 1 折成字母 I（模型输出在形近字上打滑）。
    合并成一个函数会让索引和查询锁死同一套语义 —— 以后再想修查询侧，
    就得重建整个索引。
    """
    from animediator.textutil import norm, norm_query
    assert norm("OSH1") == "osh1", norm("OSH1")
    assert norm_query("OSH1") == "oshi", norm_query("OSH1")
    for s in ("葬送のフリーレン", "Sousou no Frieren", "银魂", "DanMachi 2"):
        assert norm(s) == norm_query(s), s


def test_vfs_safety_gate():
    """虚拟库不能往「看不出是我们生成的库」的非空目录里写。"""
    from pathlib import Path
    from animediator.materialize import check_target
    with tempfile.TemporaryDirectory() as d:
        check_target(Path(d) / "fresh")            # 空/不存在 → 放行
        bad = Path(d) / "notours"
        bad.mkdir()
        (bad / "用户的珍贵文件.mkv").write_text("x")
        with raises(SystemExit):                   # 非空又不是我们的 → 拦
            check_target(bad)
        ours = Path(d) / "ours" / "某番"
        ours.mkdir(parents=True)
        (ours / "bangumi.ini").write_text("[Bangumi]\nID=1\n")
        check_target(ours.parent)                  # 有 bangumi.ini → 放行
        check_target(bad, True)                    # --force 放行


def test_cli_has_all_commands():
    from animediator.cli import _build_parser, COMMANDS
    p = _build_parser()
    names = set()
    for a in p._actions:
        if hasattr(a, "choices") and a.choices and isinstance(a.choices, dict):
            names |= set(a.choices.keys())
    missing = set(COMMANDS) - names
    assert not missing, "这些命令没挂到 CLI 上：%s" % missing


def test_global_flags_work_before_and_after_subcommand():
    """`--dry-run` 写在子命令前后都得认 —— 人自然会写后面。"""
    from animediator.cli import _build_parser
    p = _build_parser()
    a = p.parse_args(["--dry-run", "materialize"])
    assert a.dry_run is True and a.cmd == "materialize"
    b = p.parse_args(["materialize", "--dry-run"])
    assert b.dry_run is True and b.cmd == "materialize"
    # 写在前面时，子命令没给的值不能把前面的覆盖掉
    assert p.parse_args(["--config", "/x.toml", "materialize"]).config == "/x.toml"


def test_work_db_schema_creates():
    from animediator import db
    with tempfile.TemporaryDirectory() as d:
        con = db.init(os.path.join(d, "t.db"))
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("unit", "ufile", "bgm_cache", "llm_meta", "anime", "name", "relate"):
            assert t in tables, "缺表 %s" % t


def test_semantic_not_configured_raises_with_help():
    """没配语义层时必须带着说明炸掉，不能静默返回 None。

    静默降级会让整条链在「什么都没识别出来」的状态下跑完，白烧几小时。
    """
    from animediator import config
    from animediator.providers import Semantic
    with tempfile.TemporaryDirectory() as d:
        cfg = config.load(_write_cfg(d))
        s = Semantic(cfg)
        assert not s.enabled
        with raises(SystemExit) as ctx:
            s.require()


def test_semantic_key_never_hardcoded():
    """密钥只能来自配置/环境变量 —— 代码里不许有任何默认 key。"""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "src", "animediator", "providers.py")
    text = open(src, encoding="utf-8").read()
    import re
    # 形如 "xxxxxx.yyyyyy" 的智谱 key、sk- 开头的 key，都不许出现
    assert not re.search(r'"[0-9a-f]{20,}\.[A-Za-z0-9]{10,}"', text), \
        "providers.py 里疑似硬编码了 API key"
    assert not re.search(r'"sk-[A-Za-z0-9]{20,}"', text), \
        "providers.py 里疑似硬编码了 API key"


def test_init_template_is_valid_toml():
    from animediator.wizard import TEMPLATE
    from animediator import config
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "animediator.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(TEMPLATE.format(
                media='[media.DISK1]\nroot = "/tmp"\ncontainer = "/m"\n',
                jf_url="http://127.0.0.1:8096", vfs_in_container="/vfs",
                backend="none", base_url="", model="", proxy=""))
        cfg = config.load(p)
        assert cfg.media["DISK1"]["root"] == "/tmp"


def test_prompt_templates_not_empty():
    """几个调出来的 prompt 一个字都不该被"顺手改"成空。

    这些 prompt 是实测调出来的，打包时最容易出的事故就是把它们弄丢。
    """
    import importlib
    checks = [
        ("animediator.identify", "PROMPT", "你是一个动漫库整理员"),
        ("animediator.recheck", None, "正片集数"),
        ("animediator.split", None, "groups"),
        ("animediator.overflow", None, "assigns"),
    ]
    for mod_name, attr, needle in checks:
        mod = importlib.import_module(mod_name)
        if attr:
            text = getattr(mod, attr)
        else:
            # 没有模块级常量的，去函数里的 docstring/源码找
            import inspect
            text = inspect.getsource(mod)
        assert needle in text, "%s 里找不到 %r —— prompt 可能被改坏了" % (mod_name, needle)


# ══════════════════ 裸跑入口 ══════════════════

def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print("  ✓ %s" % name)
        except Exception:
            fails += 1
            print("  ✗ %s" % name)
            traceback.print_exc()
    print()
    print("%d 个测试，%d 个失败" % (len(tests), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_main())
