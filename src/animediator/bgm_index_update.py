"""`animediator bgm-update` —— 只更新离线数据，不重建索引。

用途：想确认数据是不是最新、或者想在跑大流程之前先把几百 MB 的下载做掉，
又不想顺带重建索引的时候。`run` 会自动调用同一套逻辑，所以日常不需要它。

    默认      比对版本，有新一期就下载
    --offline 不联网，只报告本地状态
    --force   无视版本判断，强制重新下载
"""
from __future__ import annotations

from .config import get as get_cfg
from .bgm import fetch


def main(args=None):
    cfg = get_cfg()
    cfg.ensure_dirs()
    offline = bool(getattr(args, "offline", False)) if args is not None else False
    force = bool(getattr(args, "force", False)) if args is not None else False

    st = fetch.read_state(cfg)
    print("数据目录：%s" % cfg.data_dir)
    if cfg.dump.exists():
        print("本地文件：%s（%.0f MB）"
              % (cfg.dump, cfg.dump.stat().st_size / 1e6))
        print("数据版本：%s" % (st.get("key") or "未记录"))
        print("获取时间：%s" % (st.get("fetched_at") or "未知"))
    else:
        print("本地文件：无")

    if offline:
        return 0

    print()
    path, fresh = fetch.ensure(force=force)
    print()
    if fresh:
        print("已更新到最新一期。索引需要重建：animediator bgm-build")
    else:
        print("无需更新。")
    return 0
