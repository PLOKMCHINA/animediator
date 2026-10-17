"""离线索引的数据获取 —— 自动下载与增量更新。

════════ 为什么必须自动化 ════════
番组计划每周发布一份全量 dump。让用户自己去 GitHub 找最新那个、手动下载、
放进 data/、每星期再来一次，这不叫工具，叫把活推回去。

这个模块负责：
    查询当前最新一期的版本号
    比对本地已装入的版本，判断要不要更新
    下载（断点续传，断了从断点接上，不从头再来）
    校验文件完整性（大小 + 可解压）
    记录装入版本，供下次增量判断

════════ 为什么不"增量下载" ════════
数据源提供的是**全量快照**，每期一个独立压缩包，没有增量补丁。
所谓增量只能在**本地**做：发现版本没变就什么都不做；
版本变了就重新下载，但**重建索引时只更新变化的部分**（见 index.py 的 reuse 逻辑）。

════════ 数据源 ════════
    https://github.com/bangumi/Archive/releases/download/archive/dump-<时间戳>.zip

每期两个文件放在同一条 release 里（一个 7z 一个 zip）。zip 用标准库就能读，
7z 需要外部工具，所以只取 zip。

GitHub API 在国内经常连不上，所以：
    · 先试 API 拿最新 tag（最准）
    · API 不通时退回**猜**：按周递推文件名（发布是每周三），试最近若干周
两者都失败才报错，并且报错信息里给出可手动放置的路径。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ..config import get as get_cfg

RELEASE_API = "https://api.github.com/repos/bangumi/Archive/releases/latest"
RELEASE_BASE = ("https://github.com/bangumi/Archive/releases/download/"
                "archive/")
STATE_FILE = "dump.state.json"          # 记在 data/ 下
UA = "animediator/1.0 (offline index updater)"


# ══════════════════ HTTP ══════════════════

def _opener():
    cfg = get_cfg()
    proxy = cfg.proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    handlers = []
    if proxy and not proxy.lower().startswith("socks"):
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _socks_proxy():
    """返回配置里的 SOCKS 代理地址，没有则 None。

    ════════ 为什么不只用 urllib ════════
    标准库的 urllib **不支持 SOCKS**：拿到 `socks5h://` 会当 HTTP 代理用，
    发出去的请求直接被对端关掉，报 `Remote end closed connection` ——
    这个报错看不出真实原因，很容易被当成网络故障。

    而引入 PySocks 就破坏了"零第三方依赖"这条底线。所以：检测到 SOCKS 时
    交给系统 curl 处理（curl 天生支持 socks5h），其余情况仍走标准库。
    """
    cfg = get_cfg()
    p = cfg.proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    return p if p.lower().startswith("socks") else None


def _curl_bytes(url, timeout=30):
    """用 curl 取字节（SOCKS 代理时走这里）。没装 curl 则报错退出。"""
    import shutil
    import subprocess
    if not shutil.which("curl"):
        raise SystemExit(
            "配置了 SOCKS 代理，但系统没有 curl。\n"
            "  标准库不支持 SOCKS 协议。装一个 curl，或把代理改成 http:// 形式。")
    proxy = _socks_proxy()
    cmd = ["curl", "-sSL", "--fail", "-m", str(timeout),
           "--proxy", proxy, url]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise urllib.error.URLError(msg[:200] or ("curl 退出码 %d" % r.returncode))
    return r.stdout


def _get_bytes(url, timeout=30):
    if _socks_proxy():
        return _curl_bytes(url, timeout)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with _opener().open(req, timeout=timeout) as r:
        return r.read()


# ══════════════════ 版本发现 ══════════════════

def _api_latest():
    """查 API 拿最新一期的 (文件名, 大小, URL)。失败返回 None。"""
    try:
        d = json.loads(_get_bytes(RELEASE_API, timeout=25))
    except Exception:
        return None
    best = None
    for a in d.get("assets") or []:
        name = a.get("name") or ""
        if not name.endswith(".zip"):
            continue
        ts = re.search(r"dump-(\d{4}-\d{2}-\d{2})\.(\d+)", name)
        if not ts:
            continue
        key = ts.group(1) + ts.group(2)
        if best is None or key > best[0]:
            best = (key, name, a.get("size") or 0,
                    a.get("browser_download_url") or "")
    if not best:
        return None
    return {"key": best[0], "name": best[1], "size": best[2], "url": best[3],
            "source": "api"}


def latest_info():
    """当前最新一期。返回 dict 或 None。

    只走 API。API 不通时不猜文件名 —— 归档里每期都带一个精确到秒的时间戳
    （`dump-2026-10-14.210226Z.zip`），猜不出来。这种情况由调用方提示用户
    手动指定或配置代理。
    """
    return _api_latest()


# ══════════════════ 本地状态 ══════════════════

def _state_path(cfg=None):
    cfg = cfg or get_cfg()
    return cfg.data_dir / STATE_FILE


def read_state(cfg=None):
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(st, cfg=None):
    p = _state_path(cfg or get_cfg())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════ 下载 ══════════════════

def _human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def _download_curl(info, dest, part):
    """curl 下载，带断点续传。进度直接由 curl 输出到 stderr。"""
    import shutil
    import subprocess
    if not shutil.which("curl"):
        raise SystemExit("配置了 SOCKS 代理但没有 curl，无法下载。")
    total = info.get("size") or 0
    cmd = ["curl", "-L", "--fail", "--proxy", _socks_proxy(),
           "-m", "3600",
           "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
           "-C", "-",                       # 断点续传
           "-o", str(part),
           "--progress-bar",
           info["url"]]
    print("  下载 %s（%s）" % (info["name"], _human(total) if total else "大小未知"))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(
            "下载失败（curl 退出码 %d）。已下载的部分保留在 %s，\n"
            "  重新执行会从断点继续。" % (r.returncode, part))
    if total and part.stat().st_size < total:
        raise SystemExit("下载不完整：%s / %s，重跑可断点续传"
                         % (_human(part.stat().st_size), _human(total)))
    part.replace(dest)
    return dest


def download(info, dest, resume=True):
    """下载到 dest。支持断点续传。返回本地路径。

    断点续传用 HTTP Range：本地已有部分文件时发 `Range: bytes=N-`。
    GitHub 的 release 下载支持 Range，所以断网后重跑能从断点接着下，
    不用把几百 MB 重来一遍。

    SOCKS 代理时交给 curl —— 它原生支持 socks5h 且 --continue-at 就是断点续传。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    total = info.get("size") or 0

    if resume and part.exists() and total:
        have = part.stat().st_size
        if have >= total:
            part.replace(dest)
            return dest

    if _socks_proxy():
        return _download_curl(info, dest, part)

    req = urllib.request.Request(info["url"])
    req.add_header("User-Agent", UA)
    start = part.stat().st_size if (resume and part.exists()) else 0
    if start:
        req.add_header("Range", "bytes=%d-" % start)
    else:
        start = 0
        if part.exists():
            part.unlink()

    t0 = time.time()
    last = 0.0
    with _opener().open(req, timeout=60) as r:
        mode = "ab" if start else "wb"
        got = start
        with open(part, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 2.0:
                    last = now
                    if total:
                        pct = 100.0 * got / total
                        speed = got / max(0.1, now - t0)
                        print("\r  %5.1f%%  %s / %s  %s/s"
                              % (pct, _human(got), _human(total), _human(speed)),
                              end="", flush=True)
                    else:
                        print("\r  %s" % _human(got), end="", flush=True)
    print()

    if total and part.stat().st_size < total:
        raise SystemExit("下载不完整：%s / %s，重跑可断点续传"
                         % (_human(part.stat().st_size), _human(total)))
    part.replace(dest)
    return dest


def verify(path):
    """校验：能当 zip 打开，且内含预期成员。"""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception as ex:
        raise SystemExit("压缩包损坏，无法解压：%s（%s）" % (path, ex))
    need = ("subject.jsonlines", "episode.jsonlines")
    miss = [n for n in need if n not in names]
    if miss:
        raise SystemExit("压缩包内容不对，缺少 %s。可能是下到了残缺文件，删掉重下。"
                         % ", ".join(miss))
    return True


# ══════════════════ 对外：确保本地有最新 dump ══════════════════

def ensure(force=False, offline=False):
    """确保 data/dump.zip 存在且是最新一期。

    返回 (路径, 是否刚下载了新版本)。
    """
    cfg = get_cfg()
    cfg.ensure_dirs()
    st = read_state(cfg)
    dest = cfg.dump

    if offline:
        if not dest.exists():
            raise SystemExit("离线模式但 %s 不存在。" % dest)
        return dest, False

    info = latest_info()
    if info is None:
        # 查不到版本信息：本地有文件就先用着
        if dest.exists():
            print("  查不到最新版本（网络不通），沿用本地 %s" % dest)
            print("  内容版本：%s" % (st.get("key") or "未知"))
            return dest, False
        raise SystemExit(
            "下载索引数据失败，也查不到最新版本。\n"
            "  1. 检查网络（bgm 的归档放在 GitHub，国内可能需要代理，\n"
            "     在配置里设 proxy = \"http://127.0.0.1:7890\"）\n"
            "  2. 或手动下载后放到：%s\n"
            "     下载页：https://github.com/bangumi/Archive/releases" % dest)

    if not force and dest.exists() and st.get("key") == info["key"]:
        print("  已是最新：%s（%s）" % (info["name"], _human(dest.stat().st_size)))
        return dest, False

    print("  发现新一期：%s" % info["name"])
    if dest.exists() and st.get("key") and st.get("key") != info["key"]:
        print("  本地当前：%s → 更新" % st["key"])
    try:
        download(info, dest)
    except (SystemExit, Exception) as ex:
        # ★ 关键：本地已有一份可用数据时，下载失败不该让整个流程崩掉。
        #   把"更新"当成可选动作 —— 拿不到新的就继续用旧的，
        #   并明确告诉用户当前用的是哪个版本、为什么没更新。
        #   只有本地压根没数据时才真正失败。
        msg = str(ex).strip().splitlines()[0] if str(ex).strip() else type(ex).__name__
        if dest.exists():
            print()
            print("  更新失败：%s" % msg)
            print("  沿用本地已有数据（版本 %s，%.0f MB）"
                  % (st.get("key") or "未记录", dest.stat().st_size / 1e6))
            print("  下次执行会重试更新。要强制跳过联网：--offline")
            return dest, False
        raise SystemExit(
            "没有可用的离线数据，且下载失败：%s\n"
            "  · 配置代理后再试：animediator.toml 里 proxy = \"http://127.0.0.1:7890\"\n"
            "  · 或手动下载后放到：%s\n"
            "    下载页 https://github.com/bangumi/Archive/releases" % (msg, dest))
    verify(dest)
    st.update({"key": info["key"], "name": info["name"],
               "size": dest.stat().st_size,
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    write_state(st, cfg)
    print("  已保存 %s" % dest)
    return dest, True
