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
    """每个命令都必须能被解析到：要么在顶层，要么属于某个分组。"""
    from animediator.cli import COMMANDS, GROUPS, TOP_LEVEL
    reachable = set(TOP_LEVEL)
    for _, members in GROUPS.values():
        reachable |= set(members)
    # 顶层里除分组名之外的才是命令
    reachable -= set(GROUPS)
    missing = set(COMMANDS) - reachable
    assert not missing, "这些命令没挂到 CLI 上：%s" % missing
    extra = reachable - set(COMMANDS)
    assert not extra, "分组里写了不存在的命令：%s" % extra


def test_old_command_names_still_work():
    """重构后旧的一级命令名必须继续可用，否则现有脚本全废。"""
    from animediator.cli import _build_parser, _rewrite_alias
    p = _build_parser()
    cases = [
        ["scan"], ["identify"], ["resolve"], ["materialize"], ["nfo"],
        ["deploy"], ["report"], ["bgm-build"], ["meta"], ["jellyfin"],
    ]
    for argv in cases:
        rewritten = _rewrite_alias(argv)
        ns = p.parse_args(rewritten)
        got = getattr(ns, "subcmd", None) or ns.cmd
        assert got == argv[0], "%s 被解析成了 %s" % (argv[0], got)


def test_global_flags_work_before_and_after_subcommand():
    """`--dry-run` 写在命令前后都得认 —— 人自然会写后面。"""
    from animediator.cli import _build_parser, _rewrite_alias
    p = _build_parser()
    a = p.parse_args(_rewrite_alias(["--dry-run", "materialize"]))
    assert a.dry_run is True
    assert (a.subcmd or a.cmd) == "materialize"

    b = p.parse_args(_rewrite_alias(["materialize", "--dry-run"]))
    assert b.dry_run is True
    assert (b.subcmd or b.cmd) == "materialize"

    # 分组写法同样两种位置都认
    c = p.parse_args(["--dry-run", "lib", "materialize"])
    assert c.dry_run is True and c.subcmd == "materialize"
    d = p.parse_args(["lib", "materialize", "--dry-run"])
    assert d.dry_run is True and d.subcmd == "materialize"
    # 写在前面时，子命令没给的值不能把前面的覆盖掉
    assert p.parse_args(_rewrite_alias(["--config", "/x.toml", "materialize"])).config == "/x.toml"


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


def test_run_without_semantic_offers_choice():
    """`run` 在没配模型时不能直接崩，必须给出可选做法。

    用户手里可能已经有完整的判定结果，只是暂时连不上模型 ——
    直接报错会给人的印象是"这工具坏了"。
    """
    from animediator import config
    from animediator import run as run_mod
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as d:
        cfgp = _write_cfg(d)
        config.load(cfgp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_mod.main(None)
        out = buf.getvalue()
        assert rc == 2, "无模型时应返回 2，实际 %r" % rc
        assert "--no-semantic" in out, "必须提示 --no-semantic 这条路"


def test_fetch_falls_back_to_local_on_failure():
    """数据更新失败时，本地有文件就必须继续用，不能崩。

    这是最容易踩的一条：把"更新"当成硬依赖，网络一抖整个流程就废了。
    """
    from animediator.bgm import fetch
    import inspect
    src = inspect.getsource(fetch.ensure)
    assert "沿用本地" in src, "ensure() 里应有本地兜底分支"
    assert "更新失败" in src


def test_jellyfin_api_error_is_reported_not_crashed():
    """Jellyfin 返回错误时给说明，不能把错误体当列表用。

    实际踩过：api_key 失效 → 返回 {"__error__":401} → 代码去取 ["Name"] →
    抛 AttributeError，用户看到的是堆栈而不是"key 过期了"。
    """
    from animediator import jellyfin

    orig = jellyfin.call
    try:
        jellyfin.call = lambda *a, **k: {"__error__": 401, "body": "Unauthorized"}
        with raises(SystemExit):
            jellyfin._folders()

        jellyfin.call = lambda *a, **k: "not-a-list"
        with raises(SystemExit):
            jellyfin._folders()

        jellyfin.call = lambda *a, **k: [{"Name": "动漫"}, {"Name": "电影"}]
        got = jellyfin._folders()
        assert [x["Name"] for x in got] == ["动漫", "电影"]
    finally:
        jellyfin.call = orig


def test_recreate_container_skips_when_already_mounted():
    """虚拟库已经挂进容器时不再重建容器。

    重建会删掉用户自己加的挂载和环境变量，不能当成默认行为。
    """
    from animediator import jellyfin, config
    with tempfile.TemporaryDirectory() as d:
        cfgp = _write_cfg(d)
        config.load(cfgp)
        cfg = config.get()
        cfg.vfs = d                    # 存在的目录
        vfs = str(cfg.vfs)

        orig = jellyfin._container_mounts
        try:
            jellyfin._container_mounts = lambda name: [vfs, "/other"]
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = jellyfin.recreate_container(None)
            assert rc == 0, "已挂载时应直接返回 0，实际 %r" % rc
            assert "跳过重建" in buf.getvalue()

            jellyfin._container_mounts = lambda name: ["/elsewhere"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = jellyfin.recreate_container(None)
            out = buf.getvalue()
            assert rc == 2, "没挂载又没授权时应停下，实际 %r" % rc
            assert "--recreate-container" in out, "必须告诉用户怎么授权"
        finally:
            jellyfin._container_mounts = orig


def test_init_refuses_overwrite_without_force():
    """已有配置时 init 不覆盖，但要告诉用户 --force 这条路。"""
    from animediator import wizard, config
    import io
    with tempfile.TemporaryDirectory() as d:
        cfgp = os.path.join(d, "animediator.toml")
        with open(cfgp, "w", encoding="utf-8") as f:
            f.write("work='w'\n")
        config.load(cfgp)

        class A:
            config = cfgp
            detect = False
            force = False

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wizard.init_cmd(A())
        assert rc == 1
        assert "--force" in buf.getvalue()


if __name__ == "__main__":
    sys.exit(_main())
