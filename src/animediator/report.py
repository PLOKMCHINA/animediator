"""报告 —— 验收层：收录情况 + 集级实测 + HTML 报告 + 清单。

════════ 在流水线里的位置 ════════
    ⑥ 出库（materialize / nfo） → ⑦ 上线（deploy） → ⑧ 验收（本模块）

四个来源拼一张图：
    status()     虚拟库目录 + bgm 索引 + unit 表 → work/status.json
    ep_enrich()  问 Jellyfin 要集级真实状态（有没有集号/中文名/ProviderIds）
    ep_split()   按季拆开统计：正片(Season>=1) vs 特典(Season 0)
    html()       status.json → 自包含单文件 HTML（数据内嵌，不依赖任何服务）
    listing()    status.json → csv / md 清单（方便人读的版本）

五个函数各自对应旧工程的一个独立脚本，逻辑一行不改地迁过来的；
main() 只是按顺序把它们串起来跑一遍。
"""
from __future__ import annotations

import csv
import html as _html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

from .config import get as get_cfg


# ─────────────────────────────────────────────────────────────────────────────
# 收录情况统计 → work/status.json
# （原 agg_status.py）
# ─────────────────────────────────────────────────────────────────────────────
def status(args=None):
    cfg = get_cfg()
    VFS = str(cfg.vfs)
    DB = str(cfg.db)
    BGM = str(cfg.bgm_db)
    status_path = str(cfg.status_json)
    EPRE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})\s*$")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(unit)")]
    print("unit 表列:", cols)

    units = [dict(r) for r in con.execute("SELECT * FROM unit")]
    print("unit %d 行" % len(units))

    b = sqlite3.connect(BGM)
    b.row_factory = sqlite3.Row

    series = []
    for d in sorted(os.listdir(VFS)):
        base = os.path.join(VFS, d)
        if not os.path.isdir(base):
            continue
        ini = os.path.join(base, "bangumi.ini")
        m = re.search(r"^ID=(\d+)", open(ini, encoding="utf-8").read(), re.M) if os.path.isfile(ini) else None
        bid = int(m.group(1)) if m else 0
        sm = b.execute("SELECT id,name,name_cn,summary,date,score,platform,tags "
                       "FROM subject WHERE id=?", (bid,)).fetchone() if bid else None
        n_main = n_sp = 0
        for sn in ("Season 00", "Season 01"):
            p = os.path.join(base, sn)
            if not os.path.isdir(p):
                continue
            c = sum(1 for f in os.listdir(p)
                    if not f.endswith((".nfo", ".ini")) and EPRE.search(os.path.splitext(f)[0]))
            if sn == "Season 00":
                n_sp = c
            else:
                n_main += c
        # 该 bgm 条目在本地 episode 表里有多少正片/SP
        bm = b.execute("SELECT COUNT(*) FROM episode WHERE subject_id=? AND type=0", (bid,)).fetchone()[0]
        bsp = b.execute("SELECT COUNT(*) FROM episode WHERE subject_id=? AND type=1", (bid,)).fetchone()[0]
        # 这一部对应哪些 unit
        us = [u for u in units if u.get("bgm_id") == bid]
        series.append({
            "dir": d, "bgm_id": bid,
            "name": (sm["name"] if sm else ""),
            "name_cn": (sm["name_cn"] if sm else ""),
            "year": (sm["date"][:4] if sm and sm["date"] else ""),
            "summary_len": len(sm["summary"] or "") if sm else 0,
            "score": sm["score"] if sm else None,
            "eps_main_local": n_main, "eps_sp_local": n_sp,
            "eps_bgm_main": bm, "eps_bgm_sp": bsp,
            "units": [{"path": u.get("path"), "bgm_id": u.get("bgm_id")} for u in us],
        })

    print("番目录 %d" % len(series))

    # 诊断分类
    issues = {"no_summary": [], "main_mismatch": [], "sp_only": [], "no_bgm": [],
              "no_main": []}
    for s in series:
        if not s["bgm_id"]:
            issues["no_bgm"].append(s["dir"])
            continue
        if s["summary_len"] == 0:
            issues["no_summary"].append(s["dir"])
        if s["eps_main_local"] == 0:
            issues["no_main"].append(s["dir"])
        if s["eps_main_local"] and s["eps_bgm_main"] and s["eps_main_local"] != s["eps_bgm_main"]:
            issues["main_mismatch"].append(
                {"dir": s["dir"], "local": s["eps_main_local"], "bgm": s["eps_bgm_main"]})

    out = {"series": series, "issues": issues,
           "stats": {"series": len(series),
                     "main": sum(s["eps_main_local"] for s in series),
                     "sp": sum(s["eps_sp_local"] for s in series)}}
    json.dump(out, open(status_path, "w"), ensure_ascii=False, indent=1)
    print()
    print("有简介 %d / %d" % (sum(1 for s in series if s["summary_len"] > 0), len(series)))
    print("无 bgm id: %d" % len(issues["no_bgm"]))
    print("正片集数不一致: %d" % len(issues["main_mismatch"]))
    print("无正片: %d" % len(issues["no_main"]))
    print("→ %s" % status_path)


# ─────────────────────────────────────────────────────────────────────────────
# 问 Jellyfin 要集级真实状态，并进 work/status.json（按番目录）
# （原 ep_enrich.py）
# ─────────────────────────────────────────────────────────────────────────────
def _jf_auth(cfg):
    """拼 Jellyfin 请求头。没配 api_key 就返回 None（调用方跳过并说明）。"""
    tok = (cfg.jellyfin.get("api_key") or "").strip()
    if not tok:
        return None
    return {"Authorization": 'MediaBrowser Token="%s"' % tok, "Accept": "application/json"}


def _jf_get(cfg, p, **q):
    u = cfg.jellyfin["url"] + p + (("?" + urllib.parse.urlencode(q)) if q else "")
    H = _jf_auth(cfg)
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=300).read().decode())


def ep_enrich(args=None):
    cfg = get_cfg()
    H = _jf_auth(cfg)
    if not H:
        print("跳过 ep_enrich：配置里没有 jellyfin.api_key，无法问 Jellyfin 要集级实测状态。")
        print("  在 animediator.toml 的 [jellyfin] 里补上 api_key（Jellyfin 控制台 → API 密钥）。")
        return
    CJK = re.compile(r"[\u4e00-\u9fff]")
    LIB = str(cfg.jellyfin.get("library_id") or cfg.jellyfin.get("library") or "")
    status_path = str(cfg.status_json)

    D = json.load(open(status_path, encoding="utf-8"))
    by_dir = {s["dir"]: s for s in D["series"]}

    series = _jf_get(cfg, "/Items", parentId=LIB, recursive="true", includeItemTypes="Series",
                     limit="500", fields="Path,ProviderIds").get("Items") or []

    matched = 0
    for s in series:
        path = s.get("Path") or ""
        d = path.rstrip("/").split("/")[-1]
        rec = by_dir.get(d)
        if not rec:
            continue
        matched += 1
        rec["jf_id"] = s["Id"]
        rec["jf_providers"] = s.get("ProviderIds") or {}
        main = {"n": 0, "id": 0, "cn": 0, "idx": 0}
        sp = {"n": 0, "id": 0, "cn": 0, "idx": 0}
        for sz in _jf_get(cfg, "/Items", parentId=s["Id"], recursive="false",
                          includeItemTypes="Season", limit="50").get("Items") or []:
            ix = sz.get("IndexNumber")
            if ix is None:
                continue
            tgt = sp if ix == 0 else main
            eps = _jf_get(cfg, "/Items", parentId=sz["Id"], recursive="false", includeItemTypes="Episode",
                          limit="3000", fields="ProviderIds,IndexNumber,Name").get("Items") or []
            tgt["n"] += len(eps)
            tgt["id"] += sum(1 for e in eps if e.get("ProviderIds"))
            tgt["cn"] += sum(1 for e in eps if CJK.search(e.get("Name") or ""))
            tgt["idx"] += sum(1 for e in eps if e.get("IndexNumber") is not None)
        rec["jf_main"] = main
        rec["jf_sp"] = sp

    print("匹配到 Jellyfin Series 的番目录: %d / %d" % (matched, len(D["series"])))

    tot = {"main": {"n": 0, "id": 0, "cn": 0, "idx": 0}, "sp": {"n": 0, "id": 0, "cn": 0, "idx": 0}}
    for s in D["series"]:
        for k, key in (("jf_main", "main"), ("jf_sp", "sp")):
            v = s.get(k) or {}
            for f in ("n", "id", "cn", "idx"):
                tot[key][f] += v.get(f, 0)

    D["jf_totals"] = tot
    json.dump(D, open(status_path, "w"), ensure_ascii=False, indent=1)
    print()
    for k, label in (("main", "正片"), ("sp", "特典")):
        t = tot[k]
        n = max(t["n"], 1)
        print("%s %d 集：集号 %.1f%% / ProviderIds %.1f%% / 中文集名 %.1f%%"
              % (label, t["n"], 100.0 * t["idx"] / n, 100.0 * t["id"] / n, 100.0 * t["cn"] / n))


# ─────────────────────────────────────────────────────────────────────────────
# 按季分开统计：正片(Season>=1) vs 特典(Season 0)
# （原 ep_split.py）
# ─────────────────────────────────────────────────────────────────────────────
def ep_split(args=None):
    cfg = get_cfg()
    H = _jf_auth(cfg)
    if not H:
        print("跳过 ep_split：配置里没有 jellyfin.api_key，无法问 Jellyfin 要按季统计。")
        print("  在 animediator.toml 的 [jellyfin] 里补上 api_key（Jellyfin 控制台 → API 密钥）。")
        return
    CJK = re.compile(r"[\u4e00-\u9fff]")
    FN = re.compile(r" - S\d{2}E\d{2}$")
    UID = str(cfg.jellyfin.get("user_id") or "")
    LIB = str(cfg.jellyfin.get("library_id") or cfg.jellyfin.get("library") or "")

    series = _jf_get(cfg, "/Items", parentId=LIB, recursive="true", includeItemTypes="Series", limit="500").get("Items") or []
    acc = {0: dict(eps=0, idx=0, cn=0, fn=0, id=0), 1: dict(eps=0, idx=0, cn=0, fn=0, id=0)}
    per = []

    for s in series:
        sid = s["Id"]
        rec = {"name": s.get("Name"), "s0": None, "s1": None}
        for sz in _jf_get(cfg, "/Shows/%s/Seasons" % sid, userId=UID).get("Items") or []:
            ix = sz.get("IndexNumber")
            if ix is None:
                continue
            key = 0 if ix == 0 else 1
            eps = _jf_get(cfg, "/Shows/%s/Episodes" % sid, userId=UID, seasonId=sz["Id"],
                          limit="3000", fields="ProviderIds,IndexNumber,Name").get("Items") or []
            d = acc[key]
            n_id = sum(1 for e in eps if e.get("ProviderIds"))
            n_cn = sum(1 for e in eps if CJK.search(e.get("Name") or ""))
            n_fn = sum(1 for e in eps if FN.search((e.get("Name") or "").strip()))
            d["eps"] += len(eps)
            d["idx"] += sum(1 for e in eps if e.get("IndexNumber") is not None)
            d["cn"] += n_cn
            d["fn"] += n_fn
            d["id"] += n_id
            rec["s0" if key == 0 else "s1"] = {"n": len(eps), "id": n_id, "cn": n_cn, "fn": n_fn}
        per.append(rec)

    for key, label in ((1, "正片 Season>=1"), (0, "特典 Season 0")):
        d = acc[key]
        n = max(d["eps"], 1)
        print("═══ %s ═══" % label)
        print("  集数        %6d" % d["eps"])
        print("  有 IndexNumber %6d  (%.1f%%)" % (d["idx"], 100.0 * d["idx"] / n))
        print("  有 ProviderIds %6d  (%.1f%%)" % (d["id"], 100.0 * d["id"] / n))
        print("  中文集名    %6d  (%.1f%%)" % (d["cn"], 100.0 * d["cn"] / n))
        print("  仍是文件名  %6d  (%.1f%%)" % (d["fn"], 100.0 * d["fn"] / n))
        print()

    print("═══ 正片中文名率 < 100%% 的番 ═══")
    bad = [r for r in per if r["s1"] and r["s1"]["n"] > 0 and r["s1"]["cn"] < r["s1"]["n"]]
    print("  %d 部" % len(bad))
    for r in bad[:18]:
        print("    %-38s %d 集，中文名 %d" % ((r["name"] or "")[:38], r["s1"]["n"], r["s1"]["cn"]))


# ─────────────────────────────────────────────────────────────────────────────
# work/status.json → 自包含单文件 HTML 报告（数据内嵌，不依赖任何服务）
# （原 mk_report.py）
# ─────────────────────────────────────────────────────────────────────────────
def html(args=None):
    cfg = get_cfg()
    D = json.load(open(str(cfg.status_json), encoding="utf-8"))
    S = D["series"]
    IS = D["issues"]
    ST = D["stats"]

    bad = set()
    for x in IS["main_mismatch"]:
        bad.add(x["dir"])
    for x in IS["no_summary"]:
        bad.add(x)
    for x in IS["no_main"]:
        bad.add(x)
    for x in IS["no_bgm"]:
        bad.add(x)

    def cls(s):
        tags = []
        if s["dir"] in IS["no_summary"]:
            tags.append("t-nosum")
        if any(m["dir"] == s["dir"] for m in IS["main_mismatch"]):
            tags.append("t-mis")
        if s["eps_main_local"] == 0:
            tags.append("t-nomain")
        return " ".join(tags)

    JT = D.get("jf_totals") or {}

    def epcell(s):
        m = s.get("jf_main") or {}
        n = m.get("n") or 0
        if n == 0:
            return '<span class="dim">—</span>'
        cn = m.get("cn") or 0
        idn = m.get("id") or 0
        pct = 100.0 * cn / n
        k = "ok" if pct >= 99 else ("mid" if pct >= 80 else "bad")
        return '<span class="%s">%d/%d</span><span class="dim"> ·id %d</span>' % (k, cn, n, idn)

    rows = []
    for s in sorted(S, key=lambda x: (x["name_cn"] or x["name"] or x["dir"])):
        title = s["name_cn"] or s["name"] or s["dir"]
        sub = s["name"] if (s["name"] and s["name"] != title) else ""
        mm = next((m for m in IS["main_mismatch"] if m["dir"] == s["dir"]), None)
        maincell = "%d" % s["eps_main_local"]
        if mm:
            maincell = '<b class="warn">%d</b><span class="dim">/%d</span>' % (mm["local"], mm["bgm"])
        elif s["eps_bgm_main"]:
            maincell = '%d<span class="dim">/%d</span>' % (s["eps_main_local"], s["eps_bgm_main"])
        rows.append(
            '<tr class="%s" data-dir="%s">'
            '<td class="t"><div class="cn">%s</div>%s</td>'
            '<td class="y">%s</td>'
            '<td class="id"><input value="%s" size="7" class="bid" data-dir="%s"></td>'
            '<td class="n">%s</td>'
            '<td class="n">%s</td>'
            '<td class="n">%s</td>'
            '<td class="n">%s</td>'
            '<td class="n">%s</td>'
            '</tr>' % (
                cls(s), _html.escape(s["dir"]),
                _html.escape(title),
                ('<div class="ja">%s</div>' % _html.escape(sub)) if sub else "",
                s["year"] or "-",
                s["bgm_id"] or "", _html.escape(s["dir"]),
                maincell,
                s["eps_sp_local"] or "-",
                epcell(s),
                ("%.1f" % s["score"]) if s["score"] else "-",
                ('<span class="ok">简介✓</span>' if s["summary_len"] else '<span class="no">缺简介</span>'),
            ))

    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>动漫库收录报告 · animediator</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;
     font:13px/1.6 -apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}
h1{font-size:17px;margin:0;font-weight:600;color:#e6edf3}
.top{padding:16px 22px;border-bottom:1px solid #21262d;background:#12181f;
     display:flex;gap:26px;align-items:baseline;flex-wrap:wrap}
.kpi{display:flex;gap:6px;align-items:baseline}
.kpi b{font-size:19px;color:#35c5cf;font-variant-numeric:tabular-nums}
.kpi span{color:#7d8590;font-size:12px}
.bar{padding:9px 22px;border-bottom:1px solid #21262d;display:flex;gap:8px;
     align-items:center;flex-wrap:wrap;background:#0f141a}
button{background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:3px;
       padding:4px 11px;cursor:pointer;font-size:12px;font-family:inherit}
button:hover{border-color:#35c5cf;color:#35c5cf}
button.on{background:#0d2b2e;border-color:#35c5cf;color:#35c5cf}
.sp{flex:1}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;background:#12181f;text-align:left;padding:7px 10px;
   font-weight:500;color:#7d8590;font-size:11.5px;border-bottom:1px solid #21262d;
   letter-spacing:.03em}
td{padding:6px 10px;border-bottom:1px solid #161b22;vertical-align:middle}
tr:hover td{background:#12181f}
.cn{color:#e6edf3}
.ja{color:#6e7681;font-size:11.5px}
.t{max-width:420px}
.y,.n{color:#8b949e;font-variant-numeric:tabular-nums;white-space:nowrap}
.id input{background:#0d1117;border:1px solid #21262d;color:#8b949e;border-radius:3px;
          padding:2px 5px;font:inherit;font-size:12px;font-variant-numeric:tabular-nums;
          width:62px}
.id input:focus{outline:none;border-color:#35c5cf;color:#35c5cf}
.dim{color:#484f58}
.warn{color:#f0883e;font-weight:600}
.ok{color:#3fb950;font-size:11.5px}
.mid{color:#d29922}
.bad{color:#f85149}
.no{color:#8b949e;font-size:11.5px}
tr.t-mis td:first-child{border-left:2px solid #f0883e}
tr.t-nosum td:first-child{border-left:2px solid #6e7681}
tr.t-nomain td:first-child{border-left:2px solid #30363d}
.hide{display:none}
#bar{position:fixed;bottom:0;left:0;right:0;background:#12181f;border-top:1px solid #30363d;
     padding:8px 22px;display:none;gap:12px;align-items:center;font-size:12px}
#bar.show{display:flex}
#bar code{background:#0d1117;padding:2px 6px;border-radius:3px;color:#35c5cf}
</style></head><body>
<div class="top">
  <h1>动漫库收录报告</h1>
  <div class="kpi"><b>__NSERIES__</b><span>部番剧</span></div>
  <div class="kpi"><b>__NMAIN__</b><span>集正片</span></div>
  <div class="kpi"><b>__NSP__</b><span>个特典</span></div>
  <div class="kpi"><b>__NEP__</b><span>集总集数（正片+特典）</span></div>
  <div class="kpi"><b>__CNRATE__</b><span>正片中文集名率</span></div>
  <div class="kpi"><b>__NSUM__</b><span>部有简介</span></div>
  <div class="kpi"><b>__NISSUE__</b><span>部待处理</span></div>
</div>
<div class="bar">
  <button class="on" data-f="all">全部</button>
  <button data-f="issue">只看待处理</button>
  <button data-f="mis">正片集数不符</button>
  <button data-f="nosum">缺简介</button>
  <button data-f="nomain">无正片</button>
  <div class="sp"></div>
  <span style="color:#6e7681;font-size:11.5px">bgm ID 可直接改，改完点右侧导出</span>
  <button id="exp">导出改动</button>
</div>
<table><thead><tr>
  <th>番名</th><th>年份</th><th>bgm ID</th><th>正片</th><th>特典</th><th>集名中文/总</th><th>评分</th><th>元数据</th>
</tr></thead><tbody>
__ROWS__
</tbody></table>
<div id="bar"><span id="msg"></span><code id="out"></code></div>
<script>
const rows=[...document.querySelectorAll('tbody tr')];
let f='all';
function apply(){
  rows.forEach(r=>{
    let show=true;
    if(f==='issue') show=r.className!=='';
    else if(f==='mis') show=r.classList.contains('t-mis');
    else if(f==='nosum') show=r.classList.contains('t-nosum');
    else if(f==='nomain') show=r.classList.contains('t-nomain');
    r.classList.toggle('hide',!show);
  });
}
document.querySelectorAll('.bar button[data-f]').forEach(b=>{
  b.onclick=()=>{document.querySelectorAll('.bar button[data-f]').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); f=b.dataset.f; apply();};
});
const changed={};
document.querySelectorAll('.bid').forEach(inp=>{
  const orig=inp.value;
  inp.onchange=()=>{ if(inp.value!==orig){changed[inp.dataset.dir]=inp.value;} else {delete changed[inp.dataset.dir];} };
});
document.getElementById('exp').onclick=()=>{
  const n=Object.keys(changed).length;
  document.getElementById('bar').classList.add('show');
  document.getElementById('msg').textContent=n?('共 '+n+' 处改动，复制下面这行：'):'没有改动';
  document.getElementById('out').textContent=n?JSON.stringify(changed):'';
};
apply();
</script></body></html>"""

    jm = JT.get("main") or {}
    js = JT.get("sp") or {}
    cnrate = "%.1f%%" % (100.0 * jm.get("cn", 0) / max(jm.get("n", 1), 1)) if jm else "-"
    nep = (jm.get("n", 0) or 0) + (js.get("n", 0) or 0)

    page = (page.replace("__ROWS__", "\n".join(rows))
                .replace("__NSERIES__", str(ST["series"]))
                .replace("__NMAIN__", str(ST["main"]))
                .replace("__NSP__", str(ST["sp"]))
                .replace("__NEP__", str(nep) if nep else str(ST["main"] + ST["sp"]))
                .replace("__CNRATE__", cnrate)
                .replace("__NSUM__", str(sum(1 for s in S if s["summary_len"])))
                .replace("__NISSUE__", str(len(bad))))

    web = cfg.work / "web"
    os.makedirs(web, exist_ok=True)
    out_path = web / "index.html"
    open(out_path, "w", encoding="utf-8").write(page)
    print("写好 %s  %.1f KB" % (out_path, len(page.encode()) / 1024))
    print("待处理 %d 部（集数不符 %d / 缺简介 %d / 无正片 %d）" % (
        len(bad), len(IS["main_mismatch"]), len(IS["no_summary"]), len(IS["no_main"])))


# ─────────────────────────────────────────────────────────────────────────────
# work/status.json → 清单 csv / md（方便人读的版本）
# （原 mk_list.py）
# ─────────────────────────────────────────────────────────────────────────────
def listing(args=None):
    cfg = get_cfg()
    csv_name = cfg.data.get("report", {}).get("csv_name", "动漫库清单.csv")
    md_name = cfg.data.get("report", {}).get("md_name", "动漫库清单.md")
    D = json.load(open(str(cfg.status_json), encoding="utf-8"))
    S = D["series"]
    IS = D["issues"]
    mis = {m["dir"]: m for m in IS["main_mismatch"]}
    nosum = set(IS["no_summary"])
    nomain = set(IS["no_main"])

    S.sort(key=lambda x: (x["name_cn"] or x["name"] or x["dir"]))

    with open(cfg.work / csv_name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["番名(中文)", "原名", "年份", "bgmID", "正片(本地)", "正片(bgm)",
                    "特典", "评分", "有简介", "问题", "目录"])
        for s in S:
            prob = []
            if s["dir"] in mis:
                prob.append("正片集数不符 %d/%d" % (mis[s["dir"]]["local"], mis[s["dir"]]["bgm"]))
            if s["dir"] in nosum:
                prob.append("缺简介")
            if s["dir"] in nomain:
                prob.append("无正片")
            w.writerow([s["name_cn"] or s["dir"], s["name"], s["year"], s["bgm_id"],
                        s["eps_main_local"], s["eps_bgm_main"], s["eps_sp_local"],
                        s["score"] or "", "有" if s["summary_len"] else "无",
                        "；".join(prob), s["dir"]])

    lines = ["# 动漫库收录清单", ""]
    lines.append("共 **%d** 部 / 正片 **%d** 集 / 特典 **%d** 个；%d 部有中文简介。"
                 % (len(S), sum(s["eps_main_local"] for s in S),
                    sum(s["eps_sp_local"] for s in S), sum(1 for s in S if s["summary_len"])))
    lines.append("")
    lines.append("## 待处理（%d 部）" % (len(mis) + len(nosum) + len(nomain)))
    lines.append("")
    if mis:
        lines.append("### 正片集数对不上（%d）" % len(mis))
        lines.append("")
        lines.append("| 番名 | 本地 | bgm | 说明 |")
        lines.append("|---|---:|---:|---|")
        for d, m in sorted(mis.items()):
            s = next(x for x in S if x["dir"] == d)
            diff = m["local"] - m["bgm"]
            note = "本地多 %d" % diff if diff > 0 else "本地少 %d" % (-diff)
            lines.append("| %s | %d | %d | %s |" % (s["name_cn"] or d, m["local"], m["bgm"], note))
        lines.append("")
    if nosum:
        lines.append("### 缺简介（%d）—— bgm 本身没有，只能翻译或补" % len(nosum))
        lines.append("")
        for d in sorted(nosum):
            s = next(x for x in S if x["dir"] == d)
            lines.append("- %s（bgm %d）" % (s["name_cn"] or d, s["bgm_id"]))
        lines.append("")
    if nomain:
        lines.append("### 无正片（%d）—— 多为剧场版/OVA/短篇，本身没有 TV 集" % len(nomain))
        lines.append("")
        for d in sorted(nomain):
            s = next(x for x in S if x["dir"] == d)
            lines.append("- %s（bgm %d，特典 %d 个）" % (s["name_cn"] or d, s["bgm_id"], s["eps_sp_local"]))
        lines.append("")

    lines.append("## 全部（%d 部）" % len(S))
    lines.append("")
    lines.append("| # | 番名 | 年份 | bgm | 正片 | 特典 | 评分 |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|")
    for i, s in enumerate(S, 1):
        lines.append("| %d | %s | %s | %d | %d | %d | %s |" % (
            i, s["name_cn"] or s["dir"], s["year"] or "-", s["bgm_id"],
            s["eps_main_local"], s["eps_sp_local"], ("%.1f" % s["score"]) if s["score"] else "-"))

    open(cfg.work / md_name, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("写好 %s / %s" % (cfg.work / csv_name, cfg.work / md_name))
    print("待处理 %d 部（集数不符 %d / 缺简介 %d / 无正片 %d）"
          % (len(mis) + len(nosum) + len(nomain), len(mis), len(nosum), len(nomain)))


# ─────────────────────────────────────────────────────────────────────────────
# CLI 入口：一次把报告产出一路做完
# ─────────────────────────────────────────────────────────────────────────────
def main(args=None):
    status(args)
    ep_enrich(args)
    ep_split(args)
    html(args)
    listing(args)


if __name__ == "__main__":
    main()
