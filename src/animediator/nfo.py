"""NFO 生成器 —— 离线元数据层。

════════ 在混合方案里的位置 ════════
    MODE=online  插件全包 → **本模块只补缺口**（扫完由 fixgaps 调）
    MODE=hybrid  插件先跑，本模块补它拉不到的
    MODE=local   插件纯摆设 → **本模块全量出**

所以本模块从来不"抢"插件的活，它只在需要时落地。

════════ 写什么、不写什么 ════════
写：
    <番>/tvshow.nfo          中文名 / 原名 / 简介 / 评分 / 年份 / 首播 / 标签
                             班底（导演、脚本…）/ 声优+角色 / Bangumi ID
    <番>/Season NN/<集>.nfo   中文集名 / 日文集名 / 每集简介 / 播出日
不写：
    海报（poster.jpg） —— 图片 URL 带 hash，**离线推不出来**，必须走 API+CDN
    所以图片永远留给插件或 art.py，本模块不碰

════════ 集号怎么对 ════════
我们的 `S{sn}E{en}` 里 `en` 是**从文件名读出来的真实集号**（materialize2 落的）。
对 bgm 章节用 **`sort == en` 的数字同一性** —— 不是模糊匹配、不打分。
对不上就不写那一集（让插件去处理），**不编、不猜、不挪号**。
"""
import os
import re
import sqlite3
import sys
import xml.sax.saxutils as sx

from .config import get as get_cfg

# 番剧级 tvshow.nfo 要不要写。Series 级的元数据插件已经给全了（中文名/简介/海报/声优），
# 没必要用本地 NFO 去盖它；集级才是插件搞不定的（见 references/jellyfin-scrape-and-disk-noise.md）。
# 默认关；`--series` 开关或配置 [nfo] series = true 可打开。
# 特典（Season 00）要不要写最小 NFO 钉住集号。默认写；配置 [nfo] special = false 可关。


def esc(s):
    return sx.escape(str(s)) if s is not None else ""


def nfo_series(con, sid):
    r = con.execute("SELECT id,name,name_cn,summary,date,score,rank,tags "
                    "FROM subject WHERE id=?", (sid,)).fetchone()
    if not r:
        return None
    _id, name, name_cn, summary, date, score, rank, tags = r
    title = (name_cn or name or "").strip()
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', "<tvshow>"]
    lines.append("  <title>%s</title>" % esc(title))
    if name:
        lines.append("  <originaltitle>%s</originaltitle>" % esc(name))
    lines.append("  <sorttitle>%s</sorttitle>" % esc(title))
    if summary:
        lines.append("  <plot>%s</plot>" % esc(summary))
    if score:
        lines.append("  <rating>%s</rating>" % esc(score))
    if date:
        y = date[:4]
        if y.isdigit():
            lines.append("  <year>%s</year>" % y)
        lines.append("  <premiered>%s</premiered>" % esc(date))
        lines.append("  <releasedate>%s</releasedate>" % esc(date))
    # 标签当 genre（bgm 的 tag 是用户打的，比 TMDB 的 genre 更贴中文习惯）
    seen = set()
    for t in (tags or "").split(","):
        t = t.strip()
        if t and t not in seen and len(t) <= 20:
            seen.add(t)
            lines.append("  <genre>%s</genre>" % esc(t))
            if len(seen) >= 10:
                break
    lines.append('  <uniqueid type="Bangumi" default="true">%d</uniqueid>' % _id)
    lines.append("  <bangumiid>%d</bangumiid>" % _id)

    # ── 班底 ──
    # 职位编号**按作品类型分域**（动画 2004 / 游戏 2004 含义不同），
    # 所以必须带 scope='anime'。实测动画范围内覆盖率 100.0%。
    staff = {}
    for pid, pcn, pname, position in con.execute(
            "SELECT p.id,p.name_cn,p.name,sp.position FROM subject_person sp "
            "JOIN person p ON p.id=sp.person_id WHERE sp.subject_id=?", (sid,)):
        nm = (pcn or pname or "").strip()
        if not nm:
            continue
        role = con.execute("SELECT cn,cat FROM staff_name WHERE id=? AND scope='anime'",
                           (position,)).fetchone()
        cat = (role[1] if role else "") or ""
        # 只取"导演类/脚本类"进 director/credits，其余当 tag 不塞
        if "导演" in cat or "演出" in cat:
            staff.setdefault("director", [])
            if nm not in staff["director"]:
                staff["director"].append(nm)
        elif "脚本" in cat or "分镜" in cat:
            staff.setdefault("credits", [])
            if nm not in staff["credits"]:
                staff["credits"].append(nm)
        elif "制作" in cat or "制片" in cat:
            staff.setdefault("studio", [])
            if nm not in staff["studio"]:
                staff["studio"].append(nm)
    for k in ("director", "credits", "studio"):
        for nm in staff.get(k, [])[:12]:
            lines.append("  <%s>%s</%s>" % (k, esc(nm), k))

    # ── 声优 + 角色 ──
    n_act = 0
    for vcn, vname, ccn, cname, ordv in con.execute(
            "SELECT p.name_cn,p.name,c.name_cn,c.name,sc.ord FROM subject_character sc "
            "JOIN character c ON c.id=sc.character_id "
            "LEFT JOIN person_character pc ON pc.character_id=c.id AND pc.subject_id=sc.subject_id "
            "LEFT JOIN person p ON p.id=pc.person_id "
            "WHERE sc.subject_id=? ORDER BY sc.ord LIMIT 40", (sid,)):
        cnm = (ccn or cname or "").strip()
        vnm = (vcn or vname or "").strip()
        if not cnm and not vnm:
            continue
        lines.append("  <actor>")
        if vnm:
            lines.append("    <name>%s</name>" % esc(vnm))
        if cnm:
            lines.append("    <role>%s</role>" % esc(cnm))
        lines.append("    <type>Actor</type>")
        lines.append("  </actor>")
        n_act += 1
    lines.append("</tvshow>")
    return "\n".join(lines) + "\n", n_act


# ⚠️ 每集 description 当初没存进索引。实地抽样看过：bgm 的 episode.description
#    在多数集里是**制作班底**（「シナリオ/大河内 一楼、コンテ/須永司、演出/三宅和男」），
#    不是剧情简介。所以不值得为它重解析 340MB —— Jellyfin 插件拉到的也是同一段文字。
#    真需要时重跑 bgm_index.py 的 episode 段加回该列即可。
def nfo_episode(ep):
    _id, name, name_cn, airdate, duration, sn, en = ep
    title = (name_cn or name or "").strip()
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', "<episodedetails>"]
    lines.append("  <title>%s</title>" % esc(title))
    if name_cn and name:
        lines.append("  <originaltitle>%s</originaltitle>" % esc(name))
    if airdate:
        lines.append("  <aired>%s</aired>" % esc(airdate))
    lines.append("  <season>%d</season>" % sn)
    lines.append("  <episode>%d</episode>" % en)
    lines.append('  <uniqueid type="Bangumi" default="true">%d</uniqueid>' % _id)
    lines.append("</episodedetails>")
    return "\n".join(lines) + "\n"


EPRE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})\s*$")


def nfo_special(en):
    """特典的最小 NFO：只钉死季号和集号，不编集名。

    Jellyfin 的 EpisodeNfoParser 认 <season> → ParentIndexNumber、
    <episode> → IndexNumber。有了这两个，集就不会掉进「未知季」空壳。
    """
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
            "<episodedetails>\n"
            "  <season>0</season>\n"
            "  <episode>%d</episode>\n"
            "</episodedetails>\n" % en)


def main(args=None):
    cfg = get_cfg()
    nfo_cfg = cfg.data.get("nfo", {}) or {}
    dry = not (getattr(args, "write", False) if args is not None else "--write" in sys.argv)
    series_flag = bool(getattr(args, "series", False)) if args is not None else False
    WRITE_SERIES = series_flag or bool(nfo_cfg.get("series", False))
    WRITE_SPECIAL = bool(nfo_cfg.get("special", True))
    BGM = str(cfg.bgm_db)
    VFS = str(cfg.vfs)
    con = sqlite3.connect(BGM)
    dirs = sorted(d for d in os.listdir(VFS) if os.path.isdir(os.path.join(VFS, d)))
    print("虚拟库 %s，%d 个番目录%s\n" % (VFS, len(dirs), "（**dry-run，不写文件**）" if dry else "（写入）"))

    st = {"番": 0, "无ini": 0, "无条目": 0, "集": 0, "集对上": 0,
          "正片对不上": 0, "特典": 0, "无集号": 0, "声优": 0,
          "对不上例": [], "无集号例": []}
    miss = []
    for d in dirs:
        base = os.path.join(VFS, d)
        ini = os.path.join(base, "bangumi.ini")
        if not os.path.isfile(ini):
            st["无ini"] += 1
            continue
        m = re.search(r"^ID=(\d+)", open(ini, encoding="utf-8").read(), re.M)
        if not m:
            st["无ini"] += 1
            continue
        sid = int(m.group(1))
        res = nfo_series(con, sid)
        if not res:
            st["无条目"] += 1
            miss.append((d, sid, "条目不存在"))
            continue
        xml, n_act = res
        st["番"] += 1
        st["声优"] += n_act
        if not dry:
            if WRITE_SERIES:
                with open(os.path.join(base, "tvshow.nfo"), "w", encoding="utf-8") as f:
                    f.write(xml)

        # 每集
        eps = {int(r[0]): r for r in con.execute(
            "SELECT sort,id,name,name_cn,airdate,duration FROM episode "
            "WHERE subject_id=? AND sort IS NOT NULL AND sort > 0", (sid,))}
        for root, _, files in os.walk(base):
            for fn in files:
                if fn.endswith(".nfo") or fn.endswith(".ini"):
                    continue
                em = EPRE.search(os.path.splitext(fn)[0])
                st["集"] += 1
                if not em:
                    st["无集号"] += 1
                    st["无集号例"].append(fn)
                    continue
                sn, en = int(em.group(1)), int(em.group(2))
                if sn == 0:
                    # Season 00 = 特典。bgm 的 SP 只收 152 条，我们本地有 4672 个
                    # （压制组打包的 NCOP/NCED/PV/CM/Menu/OST 之类 bgm 压根不收录），
                    # 数量差 30 倍，按顺序硬对必然大面积错位 → **不给集名**。
                    # 但**集号一定要写死**：不写 <season>0</season><episode>N</episode>
                    # 的话 Jellyfin 会把它们塞进「未知季」空壳，海报墙里就乱了。
                    # 集名留空 = Jellyfin 显示原始文件名（本身带发布组信息，不切不改）。
                    st["特典"] += 1
                    if not dry and WRITE_SPECIAL:
                        with open(os.path.join(root, os.path.splitext(fn)[0] + ".nfo"),
                                  "w", encoding="utf-8") as f:
                            f.write(nfo_special(en))
                    continue
                e = eps.get(en)
                if not e:
                    st["正片对不上"] += 1
                    st["对不上例"].append((d, sn, en))
                    continue
                st["集对上"] += 1
                if not dry:
                    with open(os.path.join(root, os.path.splitext(fn)[0] + ".nfo"),
                              "w", encoding="utf-8") as f:
                        f.write(nfo_episode((e[1], e[2], e[3], e[4], e[5], sn, en)))
        if st["番"] % 30 == 0:
            print("  已处理 %d 部..." % st["番"], flush=True)

    print("\n番目录 %d：出 NFO %d / 无 ini %d / 条目缺失 %d" % (
        len(dirs), st["番"], st["无ini"], st["无条目"]))
    zheng = st["集对上"] + st["正片对不上"]
    print("集文件 %d：" % st["集"])
    print("   正片 %d → 对上 %d (%.1f%%) / 对不上 %d" % (
        zheng, st["集对上"], 100.0 * st["集对上"] / max(1, zheng), st["正片对不上"]))
    print("   特典 %d（只钉集号，集名留给原始文件名）" % st["特典"])
    print("   无集号 %d" % st["无集号"])
    print("写进 tvshow.nfo 的声优/角色 %d 条" % st["声优"])
    if st["正片对不上"]:
        print("\n正片对不上的样例（前 12）：")
        for d, sn, en in st["对不上例"][:12]:
            print("   %-40s S%02dE%02d" % (d[-40:], sn, en))
    if st["无集号"]:
        print("\n无集号样例（前 6）：")
        for fn in st["无集号例"][:6]:
            print("   ", fn[:88])
    if miss:
        print("\n条目缺失：")
        for d, sid, why in miss[:20]:
            print("   %-46s id=%d  %s" % (d[-46:], sid, why))
    con.close()
    if dry:
        print("\n（dry-run 结束。加 --write 才真的落盘）")


if __name__ == "__main__":
    main()
