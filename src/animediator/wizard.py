"""初始化向导 / 配置打印 / 体检 / **自动探测**。

这几个命令的存在理由很直接：这套工具要碰别人的媒体库和 Jellyfin，
**动手之前必须能一眼看清它打算干什么**。所以：
    init           生成一份带注释的配置模板（所有环境相关的东西都在这里）
    init --detect  先探测这台机器，把能猜的都猜出来再写模板
    config         打印**当前生效**的配置（含默认值和环境变量覆盖后的结果）
    doctor         逐项体检：路径在不在、索引建没建、Jellyfin 通不通、语义层能不能用

════════ 为什么要有 --detect ════════
手动配最痛的一步是 `[media]` 的两个路径：
    root       宿主上这个盘挂在哪        → 要自己 df / lsblk
    container  容器里这个盘挂在哪        → 要自己 docker inspect
两条对不上，生成的符号链接在容器里就是断的，而 Jellyfin 只会静默地什么都不显示。
所以这一步不该让人手查 —— 机器自己知道。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import DEFAULTS, get as get_cfg

TEMPLATE = """\
# ══════════════════════════════════════════════════════════════
#  AniMediator 配置
#
#  这个文件是**唯一**放环境相关信息的地方 —— 路径、盘位、Jellyfin 地址、
#  语义层后端。代码里没有任何机器专有的字面量，所以换台机器只要改这里。
#
#  相对路径以本文件所在目录为基准。
# ══════════════════════════════════════════════════════════════

# ── 目录 ──
work = "work"        # 中间产物（作业库、扫描结果、模型判断），断点重跑靠它
data = "data"        # 离线索引：dump.zip 与 bgm.db
vfs  = "vfs"         # 输出的虚拟库（只有符号链接，随时可删可重建）

# ── 盘位：每个动漫盘一条 ──
#   root       宿主上这个盘挂在哪（扫描走这里）
#   container  容器里这个盘挂在哪（**软链目标写这里**）
#   ⚠️ 链接要被容器里的 Jellyfin 解析，所以目标必须是容器视角的路径。
#      单机直跑（不经 Docker）时两个填一样。
{media}

# ── Jellyfin ──
#   api_key 在 Jellyfin 控制台 → 高级 → API 密钥 里生成
#   vfs_in_container 是本文件上面的 vfs 目录**在容器里**的路径
[jellyfin]
url              = "{jf_url}"
api_key          = ""
library          = "动漫"
vfs_in_container = "{vfs_in_container}"
library_id       = ""

# ── 语义层（可选外挂）──
#   backend = "none"   不配模型。只影响需要"看懂片名"的命令，
#                      其余命令（扫描、生成、报告）照常工作。
#   backend = "openai" 任何 OpenAI 兼容接口，本地或云端都行。
#
#   有显卡/内存够 —— 本地跑，一分钱不花：
#       backend  = "openai"
#       base_url = "http://127.0.0.1:11434/v1"     # Ollama
#       model    = "qwen2.5:14b"
#       api_key  = "ollama"
#
#   没算力 —— 接云端：
#       base_url = "https://api.deepseek.com/v1"
#       api_key  = "sk-..."
#       model    = "deepseek-chat"
#
#   ⚠️ 用**推理模型**（名字带 reason/thinking/r1）时加 disable_thinking = true，
#      否则 token 全烧在推理上，返回的正文是空的，表现为"每次都失败"。
[semantic]
backend          = "{backend}"
base_url         = "{base_url}"
api_key          = ""
model            = "{model}"
disable_thinking = false
workers          = 8      # 并发数，按机器和 API 限流调
timeout          = 240
retries          = 10

# ── 番组计划离线索引 ──
#   allow_network = false 时完全不联网：本地索引查不到就诚实记成"待处理"，
#   不会卡在等超时上。dump.zip 从 https://github.com/bangumi/Archive 取。
[bgm]
user_agent    = "animediator/1.0 (anime library organizer)"
allow_network = true

# ── 代理（可选）──
#   api.bgm.tv 和 Cloudflare 后面的服务在国内有时连不上，需要时填这里。
proxy = "{proxy}"
"""

VIDEO_HINT = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".rmvb", ".flv", ".wmv"}


# ═══════════════════ 探测 ═══════════════════

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _mounts():
    """读 /proc/mounts，挑出「像是媒体盘」的挂载点。

    判据：真实块设备（不是 tmpfs/overlay/proc/sysfs）、不是根、不是 /boot。
    然后看它底下有没有视频文件（浅层扫，最多 3 层、最多 2000 个目录）。
    """
    out = []
    seen = set()
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if not dev.startswith("/dev/"):
            continue
        if fstype not in ("ext4", "ext3", "xfs", "btrfs", "f2fs", "exfat", "vfat", "ntfs", "zfs"):
            continue
        if mnt in ("/", "/boot", "/boot/efi") or mnt in seen:
            continue
        seen.add(mnt)
        if not os.path.isdir(mnt):
            continue
        n_video, sample = _count_videos(mnt)
        out.append({"root": mnt, "fstype": fstype, "videos": n_video,
                    "sample": sample})
    out.sort(key=lambda x: -x["videos"])
    return out


def _count_videos(root, max_dirs=2000, max_depth=4):
    """浅层数一下视频文件 —— 只为判断"这个盘装的是不是片子"。"""
    n = 0
    d = 0
    sample = ""
    for dirpath, dirnames, filenames in os.walk(root):
        d += 1
        if d > max_dirs or dirpath.count(os.sep) - root.count(os.sep) > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [x for x in dirnames if not x.startswith(".")]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in VIDEO_HINT:
                n += 1
                if not sample:
                    sample = f[:60]
                if n > 50000:
                    return n, sample
    return n, sample


def _docker_media_map():
    """问 docker：哪个容器的哪个挂载看起来是媒体库 → {宿主路径: 容器路径}

    这是 --detect 最值钱的一条：手动配最容易错的就是这个映射。
    """
    if not shutil.which("docker"):
        return {}, None
    try:
        ids = _run(["docker", "ps", "-q"])
    except Exception:
        return {}, None
    if not ids:
        return {}, None
    mapping, jf_name = {}, None
    for cid in ids.split():
        raw = _run(["docker", "inspect", cid])
        if not raw:
            continue
        try:
            d = json.loads(raw)[0]
        except Exception:
            continue
        name = (d.get("Name") or "").lstrip("/")
        image = (d.get("Config", {}).get("Image") or "").lower()
        if "jellyfin" in name.lower() or "jellyfin" in image:
            jf_name = name
        for m in d.get("Mounts") or []:
            if m.get("Type") != "bind":
                continue
            src, dst = m.get("Source") or "", m.get("Destination") or ""
            if not (src and dst):
                continue
            # 只收「像媒体库」的：容器路径带 media/anime/video，或者宿主路径下真有片子
            looks = any(k in dst.lower() for k in ("/media", "anime", "/video", "/tv", "/movie"))
            if looks or os.path.isdir(src):
                mapping.setdefault(src, dst)
    return mapping, jf_name


def _probe_jellyfin(port=8096):
    """探本机 Jellyfin。返回 (url, 描述) 或 (None, 原因)"""
    import urllib.request
    for host in ("127.0.0.1", "localhost"):
        url = "http://%s:%d" % (host, port)
        try:
            with urllib.request.urlopen(url + "/System/Info/Public", timeout=4) as r:
                d = json.loads(r.read().decode())
            return "http://127.0.0.1:%d" % port, "%s %s" % (
                d.get("ServerName") or "?", d.get("Version") or "?")
        except Exception:
            continue
    return None, "8096 上没有 Jellyfin"


def _probe_models():
    """探本机有没有跑 OpenAI 兼容的模型服务。"""
    import urllib.request
    cands = [
        ("http://127.0.0.1:11434/v1", "Ollama", "ollama"),
        ("http://127.0.0.1:8080/v1", "llama.cpp / vLLM", "local"),
        ("http://127.0.0.1:8000/v1", "vLLM", "local"),
        ("http://127.0.0.1:1234/v1", "LM Studio", "lmstudio"),
    ]
    for base, what, key in cands:
        try:
            with urllib.request.urlopen(base + "/models", timeout=3) as r:
                d = json.loads(r.read().decode())
            models = [m.get("id") for m in (d.get("data") or []) if m.get("id")]
            if models:
                return {"base_url": base, "model": models[0], "api_key": key,
                        "what": what, "n": len(models)}
        except Exception:
            continue
    return None


def detect():
    """把能探测的都探一遍，返回 (fields, report_lines)。"""
    rep = []
    fields = {"media": "", "jf_url": "http://127.0.0.1:8096",
              "vfs_in_container": "/vfs", "backend": "none",
              "base_url": "", "model": "", "proxy": ""}

    # ① 盘位 + 容器映射
    mounts = _mounts()
    cmap, jf_name = _docker_media_map()
    rep.append("盘位探测：找到 %d 个像媒体盘的挂载（视频数是浅层抽样，只为判断"
               "这个盘装的是不是片子）" % len(mounts))
    media_lines = []
    idx = 0
    used_container = set()
    # 不按盘数截断 —— 有几块盘就报几块。只有挂载点多到离谱时才设上限，
    # 免得探测本身变成负担（每个挂载点最多浅扫 2000 个目录）。
    for m in mounts[:24]:
        if m["videos"] < 10:
            continue
        idx += 1
        key = "DISK%d" % idx
        cpath = cmap.get(m["root"], "")
        if cpath and cpath not in used_container:
            used_container.add(cpath)
        elif cpath:
            cpath = ""
        if not cpath:
            cpath = m["root"]      # 没容器就单机直跑，两个一样
        media_lines.append(
            "[media.%s]\nroot      = \"%s\"\ncontainer = \"%s\"\n"
            % (key, m["root"], cpath))
        rep.append("  %-34s 抽样 %d 个视频  →  容器内 %s%s"
                   % (m["root"], m["videos"], cpath,
                      "" if cpath != m["root"] else "  （没找到容器映射，按单机填）"))
    if media_lines:
        fields["media"] = "\n".join(media_lines).rstrip()
    else:
        fields["media"] = ('# 没探测到媒体盘 —— 手动填：\n'
                           '# [media.DISK1]\n'
                           '# root      = "/mnt/your-disk"\n'
                           '# container = "/media/your-disk"')

    # ② Jellyfin
    url, info = _probe_jellyfin()
    if url:
        fields["jf_url"] = url
        rep.append("Jellyfin：%s（%s）" % (url, info))
        # 虚拟库在容器里应该挂在哪 —— 看它有没有已经挂了一个类似 /vfs 的
        for src, dst in cmap.items():
            if "vfs" in dst.lower() or (jf_name and "vfs" in src.lower()):
                fields["vfs_in_container"] = dst
                rep.append("  发现已有的虚拟库挂载：%s → %s" % (src, dst))
                break
        else:
            fields["vfs_in_container"] = "/vfs"
    else:
        rep.append("Jellyfin：%s（没在跑就先把 url 留着，之后配）" % info)

    if jf_name:
        rep.append("  容器名：%s" % jf_name)

    # ③ 本地模型
    lm = _probe_models()
    if lm:
        fields["backend"] = "openai"
        fields["base_url"] = lm["base_url"]
        fields["model"] = lm["model"]
        rep.append("语义层：探测到 %s，%d 个模型，默认用 %s"
                   % (lm["what"], lm["n"], lm["model"]))
    else:
        rep.append("语义层：没探测到本地模型服务（11434/8080/8000/1234 都没响应）")

    # ④ 代理
    for p in ("http://127.0.0.1:7890", "http://127.0.0.1:7891",
              "http://127.0.0.1:1080", "http://127.0.0.1:20171"):
        port = p.rsplit(":", 1)[1]
        try:
            import socket
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                fields["proxy"] = p
                rep.append("代理：%s 在监听（bgm.tv 连不上时用得上）" % p)
                break
        except Exception:
            continue

    # ⑤ 外部数据
    data = Path("data")
    rep.append("外部数据：dump.zip %s ｜ aodb.json %s ｜ bangumi-data.json %s"
               % ("在" if (data / "dump.zip").exists() else "缺",
                  "在" if (data / "aodb.json").exists() else "缺",
                  "在" if (data / "bangumi-data.json").exists() else "缺"))
    return fields, rep


def _toml_str(value: str) -> str:
    """转义成 TOML 双引号字符串。Windows 路径里的反斜杠要照顾到。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def render_config(state: dict) -> str:
    """把向导收集到的答案渲染成配置文件。

    只写用户真正填过的东西，其余留默认值——配置文件短，用户才愿意看。
    """
    lines = [
        "# ══════════════════════════════════════════════════════════",
        "#  AniMediator 配置",
        "#",
        "#  环境相关的东西只在这一个文件里：路径、盘位、媒体服务器地址、",
        "#  语义层后端。代码里没有机器专有的字面量，换台机器只改这里。",
        "#",
        "#  相对路径以本文件所在目录为基准。",
        "# ══════════════════════════════════════════════════════════",
        "",
    ]
    lang = state.get("lang")
    if lang:
        lines.append('lang = "%s"          # 界面语言：en / zh' % lang)
    lines += [
        'work = "work"        # 中间产物：作业库、扫描结果、判定结果，断点重跑靠它',
        'data = "data"        # 离线索引：dump.zip 与 bgm.db',
        'vfs  = "%s"        # 输出的媒体库（只有符号链接，随时可删可重建）'
        % _toml_str(state.get("vfs") or "vfs"),
        "",
        "# ── 盘位：每个动漫盘一条 ──",
        "#   root       宿主上这个盘挂在哪（扫描走这里）",
        "#   container  容器里这个盘挂在哪（符号链接的目标写这里）",
        "#   不用 Docker 时两个填一样。",
    ]
    medias = state.get("media") or []
    if not medias:
        lines += ["# [media.DISK1]",
                  '# root      = "/mnt/your-disk"',
                  '# container = "/media/your-disk"']
    for idx, m in enumerate(medias, 1):
        lines += ["",
                  "[media.DISK%d]" % idx,
                  'root      = "%s"' % _toml_str(m["root"]),
                  'container = "%s"' % _toml_str(m["container"])]

    jf = state.get("jellyfin") or {}
    lines += [
        "",
        "# ── 媒体服务器 ──",
        "#   api_key 在控制台 → 高级 → API 密钥 里生成",
        "#   vfs_in_container 是上面 vfs 目录在容器里的路径",
        "[jellyfin]",
        'url              = "%s"' % _toml_str(jf.get("url") or "http://127.0.0.1:8096"),
        'api_key          = "%s"' % _toml_str(jf.get("api_key") or ""),
        'library          = "%s"' % _toml_str(jf.get("library") or "动漫"),
        'vfs_in_container = "%s"' % _toml_str(jf.get("vfs_in_container") or "/vfs"),
        'library_id       = ""',
    ]

    sem = state.get("semantic") or {"backend": "none"}
    lines += [
        "",
        "# ── 语义层（可选外挂）──",
        '#   backend = "none"   不配模型，只跑精确匹配的命令',
        '#   backend = "openai" 任何 OpenAI 兼容接口，本地或云端都行',
        "#",
        "#   用推理模型（名字带 reason/thinking/r1）时加 disable_thinking = true，",
        "#   否则 token 全烧在推理上，返回正文是空的。",
        "[semantic]",
        'backend          = "%s"' % _toml_str(sem.get("backend") or "none"),
        'base_url         = "%s"' % _toml_str(sem.get("base_url") or ""),
        'api_key          = "%s"' % _toml_str(sem.get("api_key") or ""),
        'model            = "%s"' % _toml_str(sem.get("model") or ""),
        "disable_thinking = false",
        "workers          = 8",
        "timeout          = 240",
        "retries          = 10",
        "",
        "# ── 番组计划离线索引 ──",
        "#   allow_network = false 时完全不联网，索引查不到就记成待处理。",
        "[bgm]",
        'user_agent    = "animediator/1.0 (anime library organizer)"',
        "allow_network = true",
        "",
        "# ── 代理（可选）──",
        "#   api.bgm.tv 和 Cloudflare 后面的服务在国内有时连不上。",
        'proxy = ""',
        "",
    ]
    return "\n".join(lines)


def init_cmd(args=None):
    cfg = get_cfg()
    target = (Path(args.config) if getattr(args, "config", None) and args.config
              else Path.cwd() / "animediator.toml")
    if target.exists() and not getattr(args, "force", False):
        print("已经存在：%s" % target)
        print("  要按当前磁盘重新探测并覆盖，加 --force")
        return 1

    # 能对话就进向导，不能就生成模板（管道、CI、--no-wizard）
    from . import setup_wizard
    want_wizard = (not getattr(args, "no_wizard", False)
                   and not getattr(args, "detect", False)
                   and setup_wizard.interactive())
    if want_wizard:
        state = setup_wizard.run()
        if state is None:
            print("\n已取消，没有写入任何文件。")
            return 1
        target.write_text(render_config(state), encoding="utf-8")
        print("\n已生成 %s" % target)
        print("\n接下来：")
        print("  1. animediator bgm-build   建立离线索引（会自动下载数据）")
        print("  2. animediator doctor      看还有哪项没通")
        print("  3. animediator run         跑完整条链")
        return 0

    fields = {"media": "", "jf_url": "http://127.0.0.1:8096",
              "vfs_in_container": "/vfs", "backend": "none",
              "base_url": "", "model": "", "proxy": ""}
    if getattr(args, "detect", False):
        print("═══ 探测本机 ═══")
        fields, rep = detect()
        for ln in rep:
            print("  " + ln)
        print()

    target.write_text(TEMPLATE.format(**fields), encoding="utf-8")
    print("已生成 %s\n" % target)
    if getattr(args, "detect", False):
        print("探测到的值已经写进模板了，但**还有三处要你自己填**：")
        print("  1. [jellyfin] api_key —— 控制台 → 高级 → API 密钥")
        print("  2. [semantic] api_key / model —— 用云端模型时")
        print("  3. 检查 [media] 的 container 路径是否真的是容器里看到的")
        print()
    print("接下来：")
    print("  1. 跑  animediator bgm-build   建立离线索引（会自动下载数据）")
    print("  2. 跑  animediator doctor      看还有哪项没通")
    return 0


def config_cmd(args=None):
    cfg = get_cfg()
    print(cfg.describe())
    print()
    print("（命令行 > 环境变量 AMI_* > 配置文件 > 内置默认值）")
    if not cfg.path:
        print("⚠️ 没找到配置文件，现在用的是内置默认值。跑 `animediator init` 生成一份。")
    return 0


OK = "  ✓"
BAD = "  ✗"
WARN = "  !"


def doctor_cmd(args=None):
    cfg = get_cfg()
    print("═══ 配置 ═══")
    print(cfg.describe())
    print()
    fails, warns = [], []

    print("═══ 目录 ═══")
    for label, p in (("work", cfg.work), ("data", cfg.data_dir), ("vfs", cfg.vfs)):
        if p.exists():
            n = len(list(p.iterdir())) if p.is_dir() else 0
            print("%s %-6s %s（%d 项）" % (OK, label, p, n))
        else:
            print("%s %-6s %s（不存在，第一次跑会自动建）" % (WARN, label, p))
    print()

    print("═══ 盘位 ═══")
    if not cfg.media:
        print("%s 一个都没配 —— 先跑 `animediator init --detect` 再检查 [media]" % BAD)
        fails.append("media")
    for k, m in sorted(cfg.media.items()):
        root = (m or {}).get("root", "")
        ok = bool(root) and Path(root).is_dir()
        n = ""
        if ok:
            try:
                n = "（%d 项）" % len(os.listdir(root))
            except OSError:
                pass
        cont = (m or {}).get("container") or root
        print("%s %-5s root=%s%s" % (OK if ok else BAD, k, root or "(空)", n))
        print("        container=%s%s"
              % (cont, "" if cont != root else "  （单机模式，容器里看不到这个路径）"))
        if root and not ok:
            fails.append("media.%s" % k)
            print("        ⚠️ 这个路径不存在或读不了")
    print()

    print("═══ 离线索引 ═══")
    if cfg.dump.exists():
        print("%s dump.zip %s（%.0f MB）"
              % (OK, cfg.dump, cfg.dump.stat().st_size / 1e6))
    else:
        print("%s dump.zip 不在 %s" % (WARN, cfg.dump))
        print("        跑 `animediator bgm-build` 会自动下载最新一期（约 440 MB，")
        print("        国内需要能访问 GitHub，可在配置里设 proxy）")
        warns.append("dump.zip")
    if cfg.bgm_db.exists():
        import sqlite3
        try:
            c = sqlite3.connect(str(cfg.bgm_db))
            n = c.execute("SELECT COUNT(*) FROM subject").fetchone()[0]
            ne = c.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
            print("%s bgm.db  %d 部作品 / %d 条剧集（%.0f MB）"
                  % (OK, n, ne, cfg.bgm_db.stat().st_size / 1e6))
        except Exception as ex:
            print("%s bgm.db 打不开：%s" % (BAD, ex))
            fails.append("bgm_db")
    else:
        print("%s bgm.db 还没建（%s）" % (WARN, cfg.bgm_db))
        warns.append("bgm.db")
    if cfg.db.exists():
        import sqlite3
        try:
            c = sqlite3.connect(str(cfg.db))
            nu = c.execute("SELECT COUNT(*) FROM unit").fetchone()[0]
            nf = c.execute("SELECT COUNT(*) FROM ufile").fetchone()[0]
            print("%s 作业库 %d 个单元 / %d 个文件" % (OK, nu, nf))
            try:
                na = c.execute("SELECT COUNT(*) FROM name").fetchone()[0]
                if na:
                    print("        名字索引 %d 条（aodb 已建，归库离线段更准）" % na)
                else:
                    print("        名字索引为空 —— 会退回用 bgm.db 的 97 万条别名查，"
                          "命中率略低但不影响正确性")
            except Exception:
                pass
        except Exception as ex:
            print("%s 作业库打不开：%s" % (WARN, ex))
    else:
        print("%s 作业库还没建（跑过 scan/identify/resolve 才有）" % WARN)
    print()

    print("═══ Jellyfin ═══")
    jf = cfg.jellyfin
    url = jf.get("url", "")
    if not url:
        print("%s 没配 url" % WARN)
    else:
        import urllib.request
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/System/Info/Public",
                                        timeout=8) as r:
                d = json.loads(r.read().decode())
            print("%s 服务在跑：%s %s" % (OK, d.get("ServerName"), d.get("Version")))
        except Exception as ex:
            print("%s 连不上 %s（%s）" % (BAD, url, type(ex).__name__))
            fails.append("jellyfin")
        if not jf.get("api_key"):
            print("%s 没配 api_key —— 控制台 → 高级 → API 密钥 里生成一个" % WARN)
            warns.append("jellyfin.api_key")
        else:
            # 有 key 就真查一次，别让 doctor 说通过而 deploy 撞 401
            req = urllib.request.Request(
                url.rstrip("/") + "/Library/VirtualFolders",
                headers={"Authorization": 'MediaBrowser Token="%s"' % jf["api_key"]})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    libs = json.loads(r.read().decode())
                names = [L.get("Name") for L in libs if isinstance(L, dict)]
                print("%s api_key 可用，媒体库：%s"
                      % (OK, "、".join(str(n) for n in names) or "（还没有媒体库）"))
                want = jf.get("library") or "动漫"
                if want not in names:
                    print("%s 没有名为「%s」的媒体库 —— deploy 会找不到目标库" % (WARN, want))
                    warns.append("jellyfin.library")
            except urllib.error.HTTPError as ex:
                if ex.code in (401, 403):
                    print("%s api_key 无效（HTTP %s）—— 重新生成一个" % (BAD, ex.code))
                    fails.append("jellyfin.api_key")
                else:
                    print("%s 查媒体库失败：HTTP %s" % (BAD, ex.code))
                    fails.append("jellyfin.api")
            except Exception as ex:
                print("%s 查媒体库失败：%s" % (WARN, type(ex).__name__))
                warns.append("jellyfin.api")
    print("%s 容器内虚拟库路径 %s%s"
          % (OK, jf.get("vfs_in_container", "(未配)"),
             "" if jf.get("vfs_in_container") else "  ← 必须填，否则容器里的链接全是断的"))
    print()

    print("═══ 语义层 ═══")
    from .providers import Semantic
    s = Semantic(cfg)
    if not s.enabled:
        print("%s 未配置。依赖它的命令（identify/recheck/split/overflow）不可用；" % WARN)
        print("        其余命令照常。要开就跑 `animediator config` 看 [semantic] 怎么填。")
    else:
        print("%s %s  model=%s  workers=%s" % (OK, s.s.get("base_url"),
                                               s.s.get("model"), s.s.get("workers")))
        print("    发一次真实请求试试……")
        out = s.ask('只输出 JSON：{"ok":1}', max_tokens=200)
        if out:
            print("%s 模型回话了：%s" % (OK, out.strip()[:80]))
        else:
            print("%s 模型没回话 —— 检查 base_url / api_key / model" % BAD)
            fails.append("semantic")
    print()

    print("═══ 结论 ═══")
    if fails:
        print("有 %d 项不通：%s" % (len(fails), "、".join(fails)))
        return 1
    print("没发现阻塞项。可以开始：animediator scan")
    if warns:
        print("（%d 项提醒：%s）" % (len(warns), "、".join(warns)))
        if "bgm.db" in warns or "dump.zip" in warns:
            print("   ⚠️ 离线索引还没建 —— 扫描能跑，但认片（identify）之前必须先")
            print("      `animediator bgm-build`，否则名字查不到一条。")
    return 0
