# AniMediator

把硬盘上命名混乱的动漫收藏，整理成 Jellyfin 认得出、**全中文、有海报墙**的媒体库。

```
整理前                                  整理后（Jellyfin 里）
[VCB-Studio] Fumetsu no Anata e……        葬送的芙莉莲 (2021)
  [Ma10p_1080p]/[01][Ma10p].mkv    →        ├── Season 01/
  [Ma10p_1080p]/[02][Ma10p].mkv             │   ├── 葬送的芙莉莲 - S01E01.mkv
  [SPs]/NCOP.mkv                            │   ├── 葬送的芙莉莲 - S01E02.mkv
                                            │   └── …（中文集名、简介、海报、声优）
                                            └── Season 00/
                                                └── 葬送的芙莉莲 - S00E01.mkv
```

**你的原始文件一个字节都不会动。** 整理结果是一棵由符号链接组成的虚拟库，
配错了删掉重生成就行。

---

## 它解决什么问题

Jellyfin 自带的刮削靠**文件名**猜，而中文动漫收藏的现实是：

- 文件名里塞满发布组、画质、音轨信息（`[DBD-Raws][银魂][S1][01-49][1080P][BDRip][HEVC-10bit][FLAC]`）
- 番名可能是日文、罗马音、英文、中文，还有一堆别名
- 一个目录里装两部作品（R1+R2、TV+剧场版）
- 特典/SP/NCOP 混在正片里，数量比正片还多
- 同名不同季、同季不同版本（新编集版/总集篇/剧场版总集篇）

结果就是：认错的（Charlotte 认成 Queen Charlotte）、认不出的、
卡片上显示罗马音的、简介空着的、特典全挤进 Season 01 的。

AniMediator 换个做法：**先确定"这是什么作品"，再照着作品资料铺结构。**
身份靠 Bangumi 的离线全量索引来认，认出来之后中文名、集数、简介、
每集标题、声优全都有了，Jellyfin 只需要照着读。

---

## 三条硬设计

### 1. 只读原始文件

全程不移动、不改名、不删除你的视频。输出目录里只有：

- `bangumi.ini`（告诉 Jellyfin 这条是哪部作品）
- `.nfo`（集级元数据）
- **符号链接**（指向你的原始文件）

不满意？`rm -rf` 那个输出目录，重跑一遍。原文库毫发无损。

### 2. 引擎零依赖、零模型

扫描、归库、生成、写 NFO、出报告——**全部只用 Python 标准库**
（`sqlite3` / `urllib` / `zipfile` / `tomllib`）。你的 NAS 是 J1900 还是 5090 都一样跑。

需要"看懂"的地方（认片名、判断正片还是特典）走**可插拔的语义层**：

```toml
[semantic]
backend  = "openai"
base_url = "http://127.0.0.1:11434/v1"   # 有显卡，本地跑，一分钱不花
model    = "qwen2.5:14b"
```

```toml
[semantic]
backend  = "openai"
base_url = "https://api.deepseek.com/v1"  # 没算力，接云端
api_key  = "sk-..."
model    = "deepseek-chat"
```

**引擎侧代码完全一样。** 不配也能跑——只是依赖语义层的命令会用不了，
并且会明确告诉你缺什么，而不是静默跑成"什么都没识别出来"。

### 3. 环境相关的东西只在一处

路径、盘位、Jellyfin 地址、模型后端全在 `animediator.toml`。
代码里没有任何机器专有的字面量——换台机器只要改这一个文件。

---

## 职责边界：我们只保证一件事 —— bgm id 是对的

这条线要划清楚，不然会做一堆插件本来就干得挺好的事。

| 谁干 | 干什么 |
|---|---|
| **AniMediator** | 认身份：这个目录是哪部作品（bgm id）、哪个文件是第几集、哪个是特典 |
| **Bangumi 插件** | 拿 bgm id 去取元数据：中文名、中文简介、海报、横幅、声优、角色 |
| **Jellyfin** | 渲染海报墙、播放、转码 |

**所以我们不写 `poster.jpg`，也不下图片。** 图片 URL 带 hash，
离线推不出来——那是插件联网+tools 的活，重复做只会让两边打架。
我们把 `bangumi.ini` 写对，插件自己去拉，海报墙就是对的。

```
番目录/bangumi.ini              [Bangumi] ID=106818
番目录/Season 01/bangumi.ini    ← 必须也写一份（否则集级拿不到 id）
番目录/Season 01/暗杀教室 - S01E01.nfo   ← 中文集名/简介/播出日
```

> ⚠️ **Season 目录里的那份 `bangumi.ini` 不能省。**
> Bangumi 插件的 `EpisodeProvider` 用 `LocalConfiguration.ForPath(info.Path)` 读配置，
> 读的是**文件所在目录**的 ini。只写在番目录的话，集级拿不到 id，
> 插件既匹配不上（实测 200 集 0 个 ProviderIds），中文集名也落不下来。

### 那别的影视工具能不能用？

- **Jellyfin** —— 原生支持，这是主目标。
- **Kodi / Emby / Infuse** —— 认**标准 NFO**，我们的 `episodedetails` / `tvshow` 就是 Kodi 格式。
  加 `animediator nfo --series` 会额外写 `tvshow.nfo`，系列级元数据就有了。
  **但海报还是没有**（理由同上：图是插件的活）。这几个工具会显示文字元数据 + 空海报。
- **Plex** —— 不读 NFO、不认 `bangumi.ini`（除非装第三方 XBMCnfo importer）。
  **不支持**，也不打算假装支持。

---

## 安装

```bash
git clone <this-repo>
cd animediator
pip install -e .
```

要求 Python ≥ 3.10（3.11+ 才能用 `.toml` 配置，更低版本请用 `.json`）。

如果装的时候卡在拉构建依赖（国内常见），加 `--no-build-isolation` 用系统自带的 setuptools：

```bash
pip install --no-build-isolation -e .
```

或者不装，直接跑：

```bash
PYTHONPATH=src python3 -m animediator --help
```

---

## 快速开始

### ① 生成配置

```bash
animediator init --detect
```

`--detect` 会自己探一遍这台机器再写模板：

```
盘位探测：找到 2 个像媒体盘的挂载（视频数是浅层抽样）
  /srv/dev-disk-by-uuid-3c1a…  抽样 3992 个视频  →  容器内 /media/anime2
  /srv/dev-disk-by-uuid-fe23…  抽样  546 个视频  →  容器内 /media/anime1
Jellyfin：http://127.0.0.1:8096（synapse 10.10.7）
  发现已有的虚拟库挂载：/opt/animediator/vfs11 → /vfs
语义层：探测到 Ollama，3 个模型，默认用 qwen2.5:14b
代理：http://127.0.0.1:7890 在监听
```

它探的东西正好是手动配最容易错的三处：**盘位、容器内映射、Jellyfin 地址**。
剩下要你自己填的只有 api_key 和（用云端模型时的）模型密钥。

> **`root` 和 `container` 为什么要分开？**
> 生成的符号链接会被**容器里的 Jellyfin** 读取，它看不到宿主路径。
> 单机直跑（不经 Docker）时两个填一样即可。

### ② 建离线索引

从 [bangumi/Archive](https://github.com/bangumi/Archive) 下载每周全量 dump，
放进 `data/dump.zip`，然后：

```bash
animediator bgm-build          # 约 10~20 分钟，产出 1.3 GB 的 bgm.db
animediator bgm-check          # 体检：它到底答不答得上真问题
```

**只需要这一个文件。** `aodb.json` / `bangumi-data.json` 是可选的加速项：

| 名字索引来源 | 单值命中率（真实 455 个单元实测） |
|---|---|
| 只有 dump.zip → bgm.db | 52.5% |
| 再加 aodb | 58.9% |

差的这 6 个点不影响**正确性**（没命中的单元会被送去问模型，判断本来就是模型的活），
只影响要调多少次模型。所以：

- 想少烧点配额 → 补 `data/aodb.json` 和 `data/bangumi-data.json`，跑 `animediator bgm-aodb`
- 只想快点跑起来 → 跳过，`resolve` 会自动用 bgm.db 的 97 万条别名兜底

### ③ 体检

```bash
animediator doctor
```

逐项检查：目录在不在、盘位读不读得到、索引建没建、Jellyfin 通不通、语义层能不能用。
有问题它会直接说怎么修。

### ④ 跑

```bash
animediator run --dry-run     # 先看它打算干什么
animediator run               # 真跑
```

或者分步跑（每步都幂等，断在哪步重跑哪步）：

```bash
animediator scan         # 扫盘（只读）
animediator identify     # 认片名（要语义层）
animediator resolve      # 归库
animediator recheck      # 复核误判
animediator split        # 拆多季混装目录
animediator overflow     # 溢出重排
animediator materialize  # 生成虚拟库
animediator nfo          # 写集级 NFO
animediator deploy       # 接进 Jellyfin 并扫描
animediator report       # 出报告
```

---

## 它是怎么工作的

```
① 扫盘          scan.py         目录树 → 识别单元（拿捏"发布时本来就分好的那一层"）
② 认片名        identify.py     每个单元交给模型通盘整理（看懂番名/季号/正片特典）
③ 归库          resolve.py      有序校验：按可信顺序试候选名，命中即停。不打分、不模糊匹配
④ 复核归库      reresolve.py    让模型看原始路径（含集号标注）确认身份
⑤ 三维修正      recheck/split/overflow
                                recheck   把被误判成"特典目录"的正片找回来
                                split     一个目录装两季 → 拆开归各自的条目
                                overflow  集号装不下 → 让模型带完整清单重排
⑥ 生成          materialize.py  每条目 = 扁平章节表 → Season 01（正片）+ Season 00（特典）
⑦ NFO           nfo.py          中文集名 / 简介 / 声优 → 集级 NFO
⑧ 上线          jellyfin.py     接库 → 关掉自动分组 → 修图片顺序 → 扫描
⑨ 验收          report.py       收录情况 + HTML 报告 + csv/md 清单
```

### 几个关键取舍

**为什么"有序校验"而不是打分匹配？**
打分意味着我得拍权重（可信度 × 精确度 × 集数吻合），拍错了还显示 `1.00 high`——
实测把《葬送的芙莉莲》匹配到了《悠悠式》。现在只做一件事：
按可信顺序试候选名，`WHERE norm = ?` 命中即停。没有分数、没有阈值。

**为什么特典不给集名？**
Bangumi 全库只收录了约 150 条 SP，而实际收藏里的特典（NCOP/NCED/PV/CM/Menu）
是几千个——差 30 倍，按顺序硬对必然大面积错位。
所以特典只钉住集号，集名保留原始文件名。**诚实优于好看。**

**为什么 Season 只有 01 和 00？**
Bangumi 的数据模型是**扁平**的：一个作品就是一条 1..N 的章节表，没有"季"这一层
（不同季是不同作品）。如果听模型打的 season 号，
《火影疾风传》会被切成 Season 01/02/03 三个目录——而它其实只有一条章节表。

---

## 命令一览

| 命令 | 作用 | 要语义层 |
|---|---|---|
| `init` / `config` / `doctor` | 初始化 / 看配置 / 体检 | |
| `bgm-build` / `bgm-check` / `bgm-merge` / `bgm-people` / `bgm-aodb` | 建离线索引与自检 | |
| `scan` | 扫盘（只读） | |
| `identify` | 认片名 | ✓ |
| `resolve` / `reresolve` / `reresolve-search` | 归库（三段） | ✓ |
| `recheck` / `split` / `overflow` | 三个修正通道 | ✓ |
| `apply` | 把复核结果写回（带备份/快照） | |
| `materialize` / `nfo` | 生成虚拟库 / 写 NFO | |
| `deploy` / `jellyfin` | 接进 Jellyfin | |
| `report` | 出报告和清单 | |
| `run` | 一键跑完整条链 | |

全局选项可以写在子命令前后都行：

```bash
animediator --dry-run materialize
animediator materialize --dry-run      # 等价
```

---

## 安全网

这套工具会碰到你的媒体库和 Jellyfin 配置，所以到处都有刹车：

- **`--dry-run`**：只打印将要做什么，不写任何东西。会动 Jellyfin 和虚拟库的命令尤其该先用它。
- **虚拟库有身份检查**：目标目录非空、又看不出是 AniMediator 生成的库（没有任何 `bangumi.ini`），
  会停下来要求显式 `--force`——防止把几千个目录建到别人的收藏里。
- **`apply` 双备份**：改库前写一份 JSON 备份 + 整库快照，随时可回滚。它只改 `unit.bgm_id` 一列。
- **Jellyfin 操作全程快照**：每个写操作（改库选项、加/删媒体库、改图片顺序）
  执行前先把改动前状态写成 JSON。
- **虚拟库里遇到真文件就跳过**，绝不覆盖。

---

## 已知限制

- **离线索引里没有的新番/冷门番**：`bgm.db` 是每周 dump，比新番慢一拍。
  打开 `bgm.allow_network = true` 可以在本地查不到时联网兜底。
- **特典没有中文集名**（见上面的取舍）。
- **本地收藏比 Bangumi 少集**的番，会显示成"集数不符"——这是数据事实，不是错误。
- **一个目录里真的混了两部毫无关系的作品**时，归库只能择一，另一部会显示成"无正片"。
  这类需要人工介入。
- 语义层对**超大 prompt**（60+ 个文件）会变慢，模型可能反复重试。

---

## 目录结构

```
animediator/
├── animediator.toml        配置（唯一放环境相关东西的地方）
├── data/                   dump.zip / bgm.db / 外部标题库
├── work/                   中间产物（作业库、扫描结果、模型判断）
│   ├── animediator.db      作业库：unit / ufile / bgm_cache
│   ├── tree.json           扫描结果
│   └── status.json         收录情况
├── vfs/                    输出的虚拟库 ← 只有软链、ini、nfo
└── src/animediator/
    ├── config.py           配置装配（文件 + 环境变量 + 命令行）
    ├── providers.py        语义层（离线/本地模型/云端 API，可插拔）
    ├── db.py               作业库 schema
    ├── textutil.py         文本归一化（索引侧 / 查询侧两套语义）
    ├── bgm/                离线索引（index / merge / people / meta / check / aodb）
    ├── scan.py units.py identify.py resolve.py
    ├── reresolve.py reresolve_search.py apply.py
    ├── recheck.py split.py overflow.py
    ├── materialize.py nfo.py
    ├── jellyfin.py deploy.py
    ├── report.py
    └── run.py cli.py wizard.py
```

---

## 致谢

- [Bangumi 番组计划](https://bgm.tv) — 中文元数据与每周全量 dump
- [bangumi/Archive](https://github.com/bangumi/Archive) — dump 发布
- [bangumi-data](https://github.com/bangumi-data/bangumi-data) — 跨站 ID 映射
- [Jellyfin](https://jellyfin.org) 与 [Bangumi 插件](https://github.com/kookxiang/jellyfin-plugin-bangumi)

## License

MIT
