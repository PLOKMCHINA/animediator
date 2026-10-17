"""Jellyfin 操作 —— 六个旧脚本合并进来，逻辑一行不改。

    wire(args)      ← jf_wire_library.main     把虚拟库接进 Jellyfin
    rebuild(args)   ← rebuild_library.main     干净重建媒体库（破坏性）
    grouping(args)  ← fix_grouping.main        关掉 EnableAutomaticSeriesGrouping
    images(args)    ← fix_image_providers.main 修图片 provider 顺序
    serialize(args) ← serialize_scan.main      把扫描并发压成 1（护盘）
    scan(args)      ← 触发库扫描 + 轮询进度（提炼自 deploy_jellyfin.sh / chain_rebuild.sh / poll_scan.sh）
    deploy(args)    ← deploy_jellyfin.sh       换挂载 + 重建库 + 触发扫描（上层串联在 deploy.py）

════════ 安全网（硬要求）════════
这些函数会**改用户的 Jellyfin 配置**（改库选项、删/建媒体库、改 provider
设置、改服务器配置），所以三条铁律：

  ① 每个写操作执行前，先把**改动前的状态**整份 json 快照到 cfg.work
     （`cfg.w("libopts-before-*.json")` / `cfg.w("syscfg-before-*.json")` /
     `cfg.w("jellyfin-library-options-before.json")`），随时可回滚。
  ② `FLAGS.dry_run` 为真时**一律不写**（不碰 Jellyfin、不落快照文件），
     只打印"将要改什么"。接到 cli.py 的 `--dry-run`。
  ③ 没有配置 `jellyfin.api_key` 时立刻带说明报错（SystemExit），
     绝不静默失败 —— API key 在 Jellyfin：控制台 → API 密钥 → 点 ＋ 生成。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import FLAGS, get as get_cfg

# TODO(迁移存疑): deploy_jellyfin.sh 里 docker 镜像与 PUID/PGID/代理等是
#   机器专有的硬编码，迁过来只能先原样保留为默认值（可用配置覆盖），
#   两块媒体盘的挂载改走 cfg.media。
_DOCKER_DEFAULTS = {
    "image": "swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/nyanmisaka/jellyfin:260326-amd64",
    "name": "jellyfin",
    "config_dir": "/opt/jellyfin/config",
    "cache_dir": "/opt/jellyfin/cache",
}


# ── 公共小件 ──────────────────────────────────────────────────────────

def _require_api_key():
    """③ 没有 api_key 就带说明炸掉，绝不静默失败。"""
    cfg = get_cfg()
    if not (cfg.jellyfin.get("api_key") or "").strip():
        raise SystemExit(
            "未配置 jellyfin.api_key —— 没有它不能调 Jellyfin API。\n"
            "  生成方法：打开 Jellyfin 控制台 → 「API 密钥」→ 点 ＋ 号新建一个，\n"
            "  把生成的 token 写进 animediator.toml：\n"
            "      [jellyfin]\n"
            '      api_key = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"')


def _headers(tok):
    return {"Authorization": 'MediaBrowser Token="%s"' % tok,
            "Content-Type": "application/json", "Accept": "application/json"}


def call(method, path, body=None, **q):
    """rebuild_library.call 的原样搬运（HTTPError 返回 __error__ dict，不抛）。"""
    cfg = get_cfg()
    tok = cfg.jellyfin["api_key"]
    url = cfg.jellyfin["url"] + path + \
        (("?" + urllib.parse.urlencode(q)) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(tok), method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        return {"__error__": e.code, "body": e.read().decode()[:300]}


def _snapshot(name: str, obj) -> str | None:
    """① 改动前快照。dry-run 不落盘，只说明会写到哪里。"""
    cfg = get_cfg()
    p = cfg.w(name)
    if FLAGS.dry_run:
        print("（dry-run）将快照改动前状态 → %s" % p)
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("改动前存档 → %s" % p)
    return str(p)


# ── wire  ← jf_wire_library.py ────────────────────────────────────────

def wire(args=None):
    """把 Jellyfin 的"动漫"库接到我们的虚拟库上，并让 Bangumi 插件负责刮元数据。

    做的三件事：
      ① 库路径：/config/Shokofin/VFS/<uuid>（空目录）→ 容器内虚拟库路径
      ② 打开 EnableInternetProviders（现在是 False，等于所有远程提供器都关着）
      ③ 元数据/图片提供器顺序：**Bangumi 排第一**，并把 Shoko/TMDB/OMDB 关掉 ——
         因为我们的立场是"由我们指认身份，插件照着刮"，不让别的提供器按名字乱猜。
    """
    _require_api_key()
    cfg = get_cfg()
    TOK = cfg.jellyfin["api_key"]
    BASE = cfg.jellyfin["url"]
    LIB = cfg.jellyfin["library"]
    VFS = cfg.jellyfin.get("vfs_in_container") or "/vfs"

    def api(path, method="GET", body=None):
        req = urllib.request.Request(
            BASE + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Emby-Token": TOK, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read().decode()
            return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as ex:
            return {"__error__": ex.code, "body": ex.read().decode()[:400]}

    apply = not FLAGS.dry_run
    libs = api("/Library/VirtualFolders")
    lib = next((L for L in libs if L["Name"] == LIB), None)
    if not lib:
        print("找不到库", LIB)
        return
    o = lib["LibraryOptions"]
    old_paths = [p["Path"] for p in o["PathInfos"]]
    print("库 %s（ItemId %s）" % (LIB, lib["ItemId"]))
    print("  当前路径:", old_paths)
    print("  EnableInternetProviders:", o.get("EnableInternetProviders"))

    # 备份（dry-run 只说明，不写）
    _snapshot("jellyfin-library-options-before.json", lib)

    if not apply:
        print("\n将改动：")
        print("  库路径     %s → %s" % (old_paths, VFS))
        print("  EnableInternetProviders → True，SaveLocalMetadata → False")
        print("  MetadataFetcherOrder/SeriesFetcherOrder/ImageFetcherOrder → ['Bangumi']")
        print("  禁掉其他提供器：Shoko / TheMovieDb / The Open Movie Database / TMDb")
        print("\n（dry-run 结束）")
        return

    # ① 换路径
    # ⚠️ 别分开调 /Paths 的增删：回写整个 LibraryOptions 时会把它带回旧值
    #    （实测：删了旧路径、加了 /vfs，一写 options 又变回旧路径）。
    #    直接在 options 里改 PathInfos，一次写回。
    o["PathInfos"] = [{"Path": VFS}]

    # ②③ 选项：打开远程提供器，Bangumi 排第一，别的关掉
    keep = {"Bangumi"}
    for t in o.get("TypeOptions") or []:
        t["DisabledMetadataFetchers"] = [n for n in (t.get("DisabledMetadataFetchers") or [])]
        t["DisabledImageFetchers"] = [n for n in (t.get("DisabledImageFetchers") or [])]
    o["EnableInternetProviders"] = True
    o["SaveLocalMetadata"] = False
    o["MetadataFetcherOrder"] = ["Bangumi"]
    o["ImageFetcherOrder"] = ["Bangumi"]
    o["SeriesFetcherOrder"] = ["Bangumi"]
    for t in o.get("TypeOptions") or []:
        allm = [m["Name"] for m in t.get("_all", [])] if t.get("_all") else None
        # 直接把可用列表里非 Bangumi 的都禁用（列表从 AvailableOptions 拿不到这里，
        # 所以用已知的服务器级名单）
        for other in ("Shoko", "TheMovieDb", "The Open Movie Database", "TMDb"):
            if other not in t["DisabledMetadataFetchers"]:
                t["DisabledMetadataFetchers"].append(other)
        for other in ("Shoko", "TheMovieDb"):
            if other not in t["DisabledImageFetchers"]:
                t["DisabledImageFetchers"].append(other)
    r = api("/Library/VirtualFolders/LibraryOptions", method="POST",
            body={"Id": lib["ItemId"], "LibraryOptions": o})
    print("  写回 LibraryOptions →", "ok" if not (r or {}).get("__error__") else r)

    # 复查
    libs2 = api("/Library/VirtualFolders")
    lib2 = next((L for L in libs2 if L["Name"] == LIB), None)
    o2 = lib2["LibraryOptions"]
    print("\n复查：")
    print("  路径:", [p["Path"] for p in o2["PathInfos"]])
    print("  EnableInternetProviders:", o2.get("EnableInternetProviders"))
    for t in o2.get("TypeOptions") or []:
        if t["Type"] in ("Series", "Episode", "Season"):
            print("  %-8s 禁用元数据=%s 禁用图片=%s" % (
                t["Type"], t.get("DisabledMetadataFetchers"), t.get("DisabledImageFetchers")))


# ── rebuild  ← rebuild_library.py ─────────────────────────────────────

def rebuild(args=None):
    """干净重建「动漫」媒体库。**破坏性操作**：删掉旧库再用同样参数重建。

    ════════ 为什么必须重建 ════════
    从 vfs4 换成 vfs6 时，路径不变但**内容大变**（目录名和集号全换了）。
    Jellyfin 的增量扫描在这种剧烈变化下留下了**重复的季**，而且集号解析
    成了 `SNone ENone` —— 元数据能落到 Series 级，但落不到 Episode 级。

    流程：备份 LibraryOptions → 删库 → 用同样的参数重建 → 写回 LibraryOptions → 全扫
    """
    _require_api_key()
    cfg = get_cfg()
    TOK = cfg.jellyfin["api_key"]
    HOST = cfg.jellyfin["url"]
    NAME = cfg.jellyfin["library"]
    VFS = cfg.jellyfin.get("vfs_in_container") or "/vfs"
    WORK = cfg.work

    # ── 破坏性操作安全网 ──
    libs = call("GET", "/Library/VirtualFolders")
    old = None
    for l in libs:
        if l.get("Name") == NAME:
            old = l
            break
    if not old:
        print("找不到库 %s" % NAME)
        return 1
    if FLAGS.dry_run:
        print("将删除媒体库 %s 并重建（ItemId %s）—— dry-run 停下" % (NAME, old["ItemId"]))
        o = old.get("LibraryOptions") or {}
        print("  路径     : %s" % [p.get("Path") for p in (o.get("PathInfos") or [])])
        print("  类型     : %s" % old.get("CollectionType"))
        print("  TypeOptions: %d 项" % len(o.get("TypeOptions") or []))
        return 0

    o = old.get("LibraryOptions") or {}
    WORK.mkdir(parents=True, exist_ok=True)
    snap = _snapshot("libopts-before-rebuild.json", o)
    print("已备份旧 LibraryOptions（%d 个键）" % len(o))
    print("  路径     : %s" % [p.get("Path") for p in (o.get("PathInfos") or [])])
    print("  类型     : %s" % old.get("CollectionType"))
    print("  TypeOptions: %d 项" % len(o.get("TypeOptions") or []))

    print("\n删除旧库...")
    r = call("DELETE", "/Library/VirtualFolders", name=NAME)
    print("  →", "ok" if not (isinstance(r, dict) and r.get("__error__")) else r)

    time.sleep(3)
    print("\n重建库...")
    r = call("POST", "/Library/VirtualFolders", name=NAME, collectionType="tvshows",
             paths=VFS, refreshLibrary="false")
    print("  →", "ok" if not (isinstance(r, dict) and r.get("__error__")) else r)
    time.sleep(2)

    libs2 = call("GET", "/Library/VirtualFolders")
    new = None
    for l in libs2:
        if l.get("Name") == NAME:
            new = l
            break
    if not new:
        print("❌ 重建失败：库不见了")
        return 1
    print("  新库 ItemId = %s" % new["ItemId"])

    # 写回 LibraryOptions（保留原有路径与各种开关）
    o2 = dict(new.get("LibraryOptions") or {})
    o2.update({k: v for k, v in o.items() if k != "PathInfos"})
    o2["PathInfos"] = [{"Path": VFS}]
    o2["EnableInternetProviders"] = True
    o2["SaveLocalMetadata"] = False
    r = call("POST", "/Library/VirtualFolders/LibraryOptions",
             {"Id": new["ItemId"], "LibraryOptions": o2})
    print("  写回 LibraryOptions →", "ok" if not (isinstance(r, dict) and r.get("__error__")) else r)

    # 复验
    libs3 = call("GET", "/Library/VirtualFolders")
    for l in libs3:
        if l.get("Name") == NAME:
            o3 = l.get("LibraryOptions") or {}
            print("\n复验：")
            print("  路径            ", [p.get("Path") for p in (o3.get("PathInfos") or [])])
            print("  EnableInternetProviders", o3.get("EnableInternetProviders"))
            print("  SaveLocalMetadata      ", o3.get("SaveLocalMetadata"))
            ep = [t for t in (o3.get("TypeOptions") or []) if t.get("Type") == "Episode"]
            if ep:
                print("  Episode ImageFetchers  ", ep[0].get("ImageFetchers"))

    with open(cfg.w("lib_itemid.txt"), "w") as f:
        f.write(new["ItemId"])
    print("\n新 ItemId 已写入 work/lib_itemid.txt")

    print("\n触发全库扫描...")
    r = call("POST", "/Library/Refresh")
    print("  →", r)
    return 0


# ── grouping  ← fix_grouping.py ───────────────────────────────────────

def grouping(args=None):
    """关掉 EnableAutomaticSeriesGrouping —— 否则 银魂/银魂. 这类续作的 Season 会被合并"""
    _require_api_key()

    folders = call("GET", "/Library/VirtualFolders")
    NAME = get_cfg().jellyfin["library"]
    lib = next(l for l in folders if l.get("Name") == NAME)
    opts = lib["LibraryOptions"]
    before = opts.get("EnableAutomaticSeriesGrouping")
    if FLAGS.dry_run:
        print("（dry-run）将把 %s 的 EnableAutomaticSeriesGrouping %s → False"
              % (NAME, before))
        _snapshot("libopts-before-grouping.json", opts)
        return 0
    _snapshot("libopts-before-grouping.json", opts)
    opts["EnableAutomaticSeriesGrouping"] = False
    call("POST", "/Library/VirtualFolders/LibraryOptions",
         {"Id": lib["ItemId"], "LibraryOptions": opts})
    after = call("GET", "/Library/VirtualFolders")
    chk = next(l for l in after if l.get("Name") == NAME)["LibraryOptions"]
    print("EnableAutomaticSeriesGrouping: %s → %s（回读确认 %s）"
          % (before, False, chk.get("EnableAutomaticSeriesGrouping")))
    return 0


# ── images  ← fix_image_providers.py ──────────────────────────────────

def images(args=None):
    """停掉 ffmpeg 抽帧，把剧集图片交给插件。

    ════════ 为什么 ════════
    J1900 解不动 1080p HEVC。Jellyfin 默认给**每一集**抽视频截图当缩略图，
    全部超时 → 全库扫描卡住几十分钟不动。而**元数据本身是在正常落的**，
    慢的纯粹是抽帧。

    处理：把 Episode 的图像提供器收成只留 `Bangumi`，把 ffmpeg 那两条
    （`Screen Grabber` = VideoImageProvider、`Embedded Image Extractor`）摘掉。
    海报墙用的是 Series 的海报，不受影响。

    ⚠️ 回写 LibraryOptions 必须**整份读出来改完再整份写回** ——
       之前踩过：分开调 /Library/VirtualFolders/Paths 的增删会用旧值覆盖回来。
    """
    _require_api_key()
    cfg = get_cfg()
    LIB_NAME = cfg.jellyfin["library"]
    DROP = ("Screen Grabber", "Embedded Image Extractor")
    KEEP_EXTRA = ("Bangumi",)

    if FLAGS.dry_run:
        print("（dry-run）将：① 停掉正在跑的 RefreshLibrary/ScanMediaLibrary 任务；"
              "② %s 的 Episode/Season ImageFetchers 收成只留 %s，摘掉 %s；"
              "③ EnableInternetProviders=True，SaveLocalMetadata=False"
              % (LIB_NAME, list(KEEP_EXTRA), list(DROP)))
        return 0

    # ── 先停正在跑的扫描 ──
    for t in call("GET", "/ScheduledTasks"):
        if t.get("Key") in ("RefreshLibrary", "ScanMediaLibrary") and t.get("State") == "Running":
            try:
                call("DELETE", "/ScheduledTasks/Running/%s" % t["Id"])
                print("已停止正在跑的 %s" % t.get("Name"))
            except Exception as e:
                print("停止 %s 失败：%s" % (t.get("Key"), e))

    libs = call("GET", "/Library/VirtualFolders")
    tgt = None
    for l in libs:
        if l.get("Name") == LIB_NAME:
            tgt = l
            break
    if not tgt:
        print("找不到库 %s" % LIB_NAME)
        return 1

    o = dict(tgt.get("LibraryOptions") or {})
    _snapshot("libopts-before-imagefix.json", o)

    # 现有 TypeOptions（可能是空列表 = 走服务器默认）
    tos = list(o.get("TypeOptions") or [])
    by = {t.get("Type"): dict(t) for t in tos}

    for tname in ("Episode", "Season"):
        t = by.get(tname) or {"Type": tname}
        cur = t.get("ImageFetchers")
        if cur is None:
            # 空 = 走服务器默认（含 Screen Grabber）→ 显式写一个去掉它的列表
            t["ImageFetchers"] = list(KEEP_EXTRA)
        else:
            t["ImageFetchers"] = [x for x in cur if x not in DROP]
        t.setdefault("MetadataFetchers", [])
        t.setdefault("ImageOptions", [])
        t.setdefault("MetadataFetcherOrder", [])
        t.setdefault("ImageFetcherOrder", [])
        by[tname] = t
        print("  %-8s ImageFetchers → %s" % (tname, t["ImageFetchers"]))

    order = ["Series", "Season", "Episode", "Movie"]
    o["TypeOptions"] = [by[k] for k in order if k in by] + \
                       [v for k, v in by.items() if k not in order]
    o["EnableInternetProviders"] = True   # 插件要从 bgm.tv 拉，明确打开
    o["SaveLocalMetadata"] = False        # 不烤 NFO，留给插件

    # ⚠️ body 必须包一层 {Id, LibraryOptions} —— 直接传 LibraryOptions 会 400
    call("POST", "/Library/VirtualFolders/LibraryOptions",
         {"Id": tgt["ItemId"], "LibraryOptions": o})
    print("\n已回写 LibraryOptions")
    print("  EnableInternetProviders =", o["EnableInternetProviders"])
    print("  SaveLocalMetadata       =", o["SaveLocalMetadata"])

    # 复验
    libs2 = call("GET", "/Library/VirtualFolders")
    for l in libs2:
        if l.get("Name") == LIB_NAME:
            o2 = l.get("LibraryOptions") or {}
            for t in (o2.get("TypeOptions") or []):
                if t.get("Type") in ("Episode", "Season"):
                    print("  复验 %-8s ImageFetchers = %s" % (t.get("Type"), t.get("ImageFetchers")))
    return 0


# ── serialize  ← serialize_scan.py ────────────────────────────────────

def serialize(args=None):
    """把 Jellyfin 的扫描并发压成 1 —— 让 IO 变连续，磁头不再乱窜。

    ════════ 证据 ════════
    盘吵时采到的：jellyfin 单进程 8 秒读 138MB（≈17MB/s）、两块盘同时被读、
    日志里 4 个线程并行 "Starting ffprobe"、LibraryScanFanoutConcurrency = 0
    （自动 = CPU 核数 = 4）。4 个 probe 并行 → 磁头来回找道 = 吵。
    **并发降到 1 就是"让它做连续读写"**。代价：总耗时变长，但对一块已经
    出坏道的家用盘，这个交换是值得的。

    ⚠️ 只改服务器配置，不动任何媒体文件。
    """
    _require_api_key()

    if FLAGS.dry_run:
        d = call("GET", "/System/Configuration")
        before = {k: d.get(k) for k in
                  ("LibraryScanFanoutConcurrency", "LibraryMetadataRefreshConcurrency",
                   "ParallelImageEncodingLimit", "ImageExtractionTimeoutMs")}
        print("（dry-run）将把服务器配置改为：")
        print("  LibraryScanFanoutConcurrency      %s → 1" % before["LibraryScanFanoutConcurrency"])
        print("  LibraryMetadataRefreshConcurrency %s → 1" % before["LibraryMetadataRefreshConcurrency"])
        print("  ParallelImageEncodingLimit        %s → 1" % before["ParallelImageEncodingLimit"])
        if not before["ImageExtractionTimeoutMs"]:
            print("  ImageExtractionTimeoutMs          %s → 30000" % before["ImageExtractionTimeoutMs"])
        _snapshot("syscfg-before-serialize.json", d)
        return 0

    d = call("GET", "/System/Configuration")
    _snapshot("syscfg-before-serialize.json", d)

    before = {k: d.get(k) for k in
              ("LibraryScanFanoutConcurrency", "LibraryMetadataRefreshConcurrency",
               "ParallelImageEncodingLimit", "ImageExtractionTimeoutMs")}
    print("改前:", before)

    # 0 = 自动。压成 1：一次只处理一个条目 → IO 串行化。
    d["LibraryScanFanoutConcurrency"] = 1
    d["LibraryMetadataRefreshConcurrency"] = 1
    # 图片编码也压掉（原来并行 4 条 ffmpeg 编码图片）
    d["ParallelImageEncodingLimit"] = 1

    # 抽帧超时给足，避免又出现 "ffmpeg image extraction timed out"
    if not d.get("ImageExtractionTimeoutMs"):
        d["ImageExtractionTimeoutMs"] = 30000

    call("POST", "/System/Configuration", d)

    d2 = call("GET", "/System/Configuration")
    after = {k: d2.get(k) for k in before}
    print("改后:", after)

    ok = (after["LibraryScanFanoutConcurrency"] == 1
          and after["LibraryMetadataRefreshConcurrency"] == 1)
    print("\n%s" % ("✅ 已生效（扫描并发 = 1，IO 串行化）" if ok else "❌ 没生效，复查"))
    return 0 if ok else 1


# ── scan  ← deploy_jellyfin.sh / chain_rebuild.sh / poll_scan.sh ──────

def scan(args=None):
    """触发全库扫描 + 轮询进度与 Series 元数据落地情况。

    提炼自 chain_rebuild.sh ③（触发 + 每 30s 轮询 RefreshLibrary 状态与
    有 ProviderIds 的 Series 数，Idle 即结束）和 poll_scan.sh 的轮询体。
    """
    _require_api_key()
    cfg = get_cfg()
    HOST = cfg.jellyfin["url"]
    tok = cfg.jellyfin["api_key"]
    H = _headers(tok)

    if FLAGS.dry_run:
        print("（dry-run）将 POST %s/Library/Refresh 触发全库扫描，然后每 30s 轮询进度"
              % HOST)
        return 0

    r = call("POST", "/Library/Refresh")
    print("触发扫描 →", r)

    # lib itemid：重建会写 work/lib_itemid.txt；没有就从库里现查
    lib_id = None
    p = cfg.w("lib_itemid.txt")
    if p.is_file():
        lib_id = p.read_text().strip()
    else:
        libs = call("GET", "/Library/VirtualFolders")
        for l in libs:
            if l.get("Name") == cfg.jellyfin["library"]:
                lib_id = l["ItemId"]
                break

    for i in range(1, 121):
        time.sleep(30)
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    HOST + "/ScheduledTasks", headers=H), timeout=30) as r2:
                tasks = json.loads(r2.read().decode())
            S = next(("%s %.1f%%" % (t.get("State"), t.get("CurrentProgressPercentage") or 0)
                      for t in tasks if t.get("Key") == "RefreshLibrary"), "?")
        except Exception:
            S = "?"
        N = "?"
        if lib_id:
            try:
                q = urllib.parse.urlencode({
                    "parentId": lib_id, "recursive": "true",
                    "includeItemTypes": "Series", "fields": "ProviderIds", "limit": "500"})
                with urllib.request.urlopen(urllib.request.Request(
                        HOST + "/Items?" + q, headers=H), timeout=120) as r3:
                    d = json.loads(r3.read().decode())
                it = d.get("Items") or []
                N = "%d/%d" % (sum(1 for x in it if (x.get("ProviderIds") or {})), len(it))
            except Exception:
                N = "?"
        print("   [%ds] 扫描 %s   Series 有 id %s" % (i * 30, S, N))
        if S.startswith("Idle"):
            print("   扫描结束")
            break
    return 0


# ── deploy  ← deploy_jellyfin.sh（换挂载）+ 串联 ──────────────────────

def recreate_container(args=None):
    """deploy_jellyfin.sh 的 Python 版：重建 Jellyfin 容器，把宿主 cfg.vfs 挂到 /vfs:ro。

    除 /vfs 外所有参数逐项照抄旧脚本（镜像/网络/重启策略/挂载/环境变量）。
    配置在 /opt/jellyfin/config，重建不丢。
    """
    cfg = get_cfg()
    jf = cfg.jellyfin
    name = jf.get("container_name") or _DOCKER_DEFAULTS["name"]
    image = jf.get("image") or _DOCKER_DEFAULTS["image"]
    cfg_dir = jf.get("config_dir") or _DOCKER_DEFAULTS["config_dir"]
    cache_dir = jf.get("cache_dir") or _DOCKER_DEFAULTS["cache_dir"]
    vfs_host = str(cfg.vfs)
    vfs_in = jf.get("vfs_in_container") or "/vfs"

    import os
    if not os.path.isdir(vfs_host):
        print("❌ 目录不存在：%s" % vfs_host)
        return 1
    print("把 %s 挂到：%s" % (vfs_in, vfs_host))

    if FLAGS.dry_run:
        print("（dry-run）将 docker stop/rm %s 并用镜像 %s 重建，挂载 %s:%s:ro"
              % (name, image, vfs_host, vfs_in))
        return 0

    subprocess.run(["docker", "stop", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "rm", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd = ["docker", "run", "-d", "--name", name,
           "--network", "host",
           "--restart", "unless-stopped",
           "-e", "PUID=0", "-e", "PGID=0", "-e", "TZ=Asia/Shanghai",
           "-e", "JELLYFIN_DATA_DIR=/config",
           "-e", "JELLYFIN_CACHE_DIR=/cache",
           "-e", "JELLYFIN_CONFIG_DIR=/config/config",
           "-e", "JELLYFIN_LOG_DIR=/config/log",
           "-e", "JELLYFIN_WEB_DIR=/jellyfin/jellyfin-web",
           "-e", "HTTP_PROXY=http://127.0.0.1:7890",
           "-e", "HTTPS_PROXY=http://127.0.0.1:7890",
           "-e", "http_proxy=http://127.0.0.1:7890",
           "-e", "https_proxy=http://127.0.0.1:7890",
           "-e", "NO_PROXY=localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8",
           "-e", "no_proxy=localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8",
           "-v", "%s:/config" % cfg_dir,
           "-v", "%s:/cache" % cache_dir]
    # 媒体盘挂载（旧脚本里是两块盘的硬编码 uuid → 走 cfg.media）
    for k, m in sorted((cfg.media or {}).items()):
        root = m.get("root", "")
        cont = m.get("container") or m.get("root", "")
        if root and cont:
            cmd += ["-v", "%s:%s:rslave" % (root, cont)]
    cmd += ["-v", "%s:%s:ro" % (vfs_host, vfs_in),
            image]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)
    print("容器已重建")

    print("等就绪...")
    for i in range(1, 41):
        code = "000"
        try:
            import http.client
            from urllib.parse import urlparse
            u = urlparse(cfg.jellyfin["url"])
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            conn.request("GET", "/System/Info/Public")
            code = str(conn.getresponse().status)
            conn.close()
        except Exception:
            code = "000"
        if code == "200":
            print("  第 %ds 就绪" % (i * 5))
            break
        time.sleep(5)
    print("--- 挂载确认 ---")
    r = subprocess.run(["docker", "inspect", name, "--format",
                        "{{json .HostConfig.Binds}}"],
                       capture_output=True, text=True)
    binds = json.loads(r.stdout) if r.stdout.strip() else []
    for b in binds:
        if vfs_in.strip("/") in b or b.endswith(":" + vfs_in):
            print("  ", b)
    print("--- 容器内能看到几部 ---")
    r = subprocess.run(["docker", "exec", name, "sh", "-c",
                        "ls %s | wc -l" % vfs_in],
                       capture_output=True, text=True)
    print("  ", (r.stdout or "").strip())
    return 0


def deploy(args=None):
    """上线：换挂载（重建容器）→ wire 或 rebuild → grouping → images → scan。"""
    rc = recreate_container(args)
    if rc:
        return rc
    if getattr(args, "rebuild", False):
        rc = rebuild(args)
    else:
        wire(args)
    if rc:
        return rc
    grouping(args)
    images(args)
    return scan(args)


# ── main：`animediator jellyfin <子命令>` ─────────────────────────────

_ACTIONS = {
    "wire":      ("把虚拟库接进 Jellyfin（换路径 + Bangumi 排第一）", wire),
    "rebuild":   ("干净重建媒体库（破坏性：删旧库重建）", rebuild),
    "grouping":  ("关掉 EnableAutomaticSeriesGrouping", grouping),
    "images":    ("修图片 provider 顺序（停 ffmpeg 抽帧）", images),
    "serialize": ("把扫描并发压成 1（护盘）", serialize),
    "scan":      ("触发全库扫描并轮询进度", scan),
}


def main(args=None):
    import argparse
    if args is not None and getattr(args, "jf_action", None):
        # 经由 `animediator jellyfin <子命令>` 进来，cli 已解析好位置参数
        action = args.jf_action
    else:
        # 直接 `python -m animediator.jellyfin <子命令>` 时自己解析一次
        ap = argparse.ArgumentParser(
            prog="animediator jellyfin",
            description="单独操作 Jellyfin：接线/重建/分组/图片/并发/扫描")
        sub = ap.add_subparsers(dest="jf_action", metavar="子命令")
        for name, (help_text, _) in _ACTIONS.items():
            sub.add_parser(name, help=help_text, description=help_text)
        ns = ap.parse_args()
        action = ns.jf_action
        args = ns
    if not action:
        print("用法：animediator jellyfin {wire|rebuild|grouping|images|serialize|scan}")
        return 0
    return int(_ACTIONS[action][1](args) or 0)
