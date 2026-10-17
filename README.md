# AniMediator

把磁盘上命名各异的动漫文件整理成媒体服务器能直接读取的目录树。

它扫描视频目录，判定每个目录对应 Bangumi 条目库里的哪部作品，然后生成一棵
只含符号链接的媒体目录树，并写入 `bangumi.ini` 与集级 NFO。原始视频文件不会
被移动、重命名或删除。

## 工作方式

整理分两个阶段。

**判定阶段**读取目录名和文件名，产出每个目录的归属判定。判定所需的两类
信息来自不同来源：目录自身的命名特征由语言模型识别（例如判断
`[Ma10p_1080p]` 下 25 个带编号的文件是正片，而 `[SPs]` 下的是特典），
作品到 Bangumi 条目的映射则由本地离线索引精确查询得到。

**生成阶段**依据判定结果构造输出目录。每个 Bangumi 条目对应一个目录，
下设 `Season 01`（正片）与 `Season 00`（特典）。集号取自 Bangumi 条目的
章节序号，不与文件名中的编号直接对应——发布方常用局部编号
（`001-032`）或部番编号（`113-143`），两者都会按目录名标注的区间换算。

Jellyfin 通过 `bangumi.ini` 中的 `ID` 得知归属，再经 Bangumi 插件获取中文名、
简介、海报与声优。本工具不下载图片，也不写番剧级元数据。

输出的是标准目录结构与 NFO。Jellyfin 已实测可用；其他能读本地 NFO 的客户端
理论上也能读，但没有验证过。Plex 不读本地 NFO，也不认 `bangumi.ini`，用它读
不到这里的任何元数据。

## 依赖

运行时只需 Python 3.10 或更高版本，无第三方包依赖。

`bgm-consts` 子命令解析 Stardict 格式的对照表时使用 PyYAML；其余命令不需要。

配置文件使用 TOML，需要 Python 3.11+（标准库 `tomllib`）。Python 3.10 请改用
JSON 格式的配置文件，字段相同。

## 安装

仓库自带启动脚本，克隆完就能用，不需要 pip：

```bash
git clone https://github.com/PLOKMCHINA/animediator.git
cd animediator
./animediator doctor
```

`./animediator` 只是把 `src/` 加进 `PYTHONPATH` 后调用 Python，不做任何安装。

想装成系统命令，用虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/animediator doctor
```

构建隔离要联网拉 setuptools 和 wheel。国内访问 pypi.org 经常不通，改用镜像：

```bash
.venv/bin/pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Debian 12 及以后的系统 Python 受 PEP 668 保护，直接 `pip install` 会被拒绝，
这就是上面用虚拟环境的原因。确实要装进系统 Python 才需要
`--break-system-packages`，一般没有必要。

## 配置

运行 `animediator init` 生成 `animediator.toml`。加 `--detect` 会先探测当前
机器的磁盘挂载、Docker 端口映射、Jellyfin 服务与本地模型服务，把探测结果写入
模板，减少手工填写。

```toml
work = "work"          # 中间产物目录
data = "data"          # 离线索引目录
vfs  = "vfs"           # 输出目录

[media.DISK1]
root      = "/mnt/disk1"        # 宿主机上的路径，扫描从这里开始
container = "/media/disk1"      # 容器内路径，符号链接指向这里

[jellyfin]
url              = "http://127.0.0.1:8096"
api_key          = ""
library          = "动漫"
vfs_in_container = "/vfs"

[semantic]
backend  = "none"
base_url = ""
api_key  = ""
model    = ""
workers  = 8
```

### media.root 与 media.container 的区别

`root` 是扫描入口，指向宿主机上真实的磁盘挂载点。`container` 用于生成符号
链接的目标路径：如果 Jellyfin 运行在 Docker 中，它只能看到容器内的路径，
链接指向宿主机路径会失效。直接在本机运行 Jellyfin 时，两者填相同值。

配置错误不会产生报错，症状是 Jellyfin 扫描后媒体库为空或集数异常——
这是排查此类问题时最先检查的地方。

### 配置优先级

命令行参数高于环境变量，环境变量高于配置文件，配置文件高于内置默认值。
配置项对应的环境变量沿用 `AMI_` 前缀（如 `AMI_VFS`、`AMI_BGMDb`），用于脚本调用。

### 语义层

`semantic.backend` 为 `none` 时，依赖语言模型的命令（`identify`、`reresolve`、
`recheck`、`split`、`overflow`）不可用，其余命令正常。设为 `openai` 后指向任何
兼容 OpenAI 接口的服务：

```toml
[semantic]
backend  = "openai"
base_url = "http://127.0.0.1:11434/v1"   # Ollama
model    = "qwen2.5:14b"
api_key  = "ollama"
```

使用推理模型（名称含 `reason`、`thinking` 或 `r1`）时需要设置
`disable_thinking = true`，否则输出预算会被推理过程耗尽，返回的正文为空，
表现为反复重试失败。

`workers` 控制并发请求数。免费额度较小的接口应调低该值，否则容易触发限流。

## 建立离线索引

```bash
animediator bgm-build
```

这一步不需要准备任何文件。它会自己查询番组计划归档的最新一期、下载、
校验完整性，然后建成索引。数据每周更新一次，重新执行同一条命令即可跟进。

下载支持断点续传，中断后重跑会从断点继续，不会从头再来。

数据来自 [bangumi/Archive](https://github.com/bangumi/Archive)，每期约 450 MB
压缩包，展开为约 1.3 GB 的 SQLite 数据库。索引建成后，所有作品查询都是本地
等值比较，不依赖网络。

归档放在 GitHub，国内网络可能连不上，此时在配置里填代理：

```toml
proxy = "http://127.0.0.1:7890"
```

代理支持 `http://` 与 `socks5h://` 两种形式。

若本地已有一份可用数据，更新失败不会中断流程——它会保留旧版本继续工作，
并说明当前使用的版本。只有本地完全没有数据时才会中止。

`bgm-update` 只做数据更新，不重建索引；`bgm-build --check` 只验证已有索引；
`bgm-check` 输出索引的实际查询结果。

### 可选的外部标题库

`data/aodb.json` 与 `data/bangumi-data.json` 可提高归库阶段的离线命中率，
实测单值命中率为 58.9%（含）与 52.5%（不含）。未命中不影响正确性，
这些目录会转入模型判定路径，只是增加调用次数。

不放这两个文件时，`resolve` 会自动从 `bgm.db` 的别名表重建名字索引。

## 运行完整流程

```bash
animediator run
```

一条命令跑完全部步骤，中途不需要人干预。它会依次完成：检查并更新索引数据、
扫描磁盘、判定目录内容、归属到 Bangumi 条目、复核与修正、生成输出目录、
写入 NFO、接入 Jellyfin、生成报告。

先看它打算做什么而不实际写入：

```bash
animediator run --dry-run
```

常用选项：

| 选项 | 说明 |
|---|---|
| `--from <步骤>` | 从指定步骤开始 |
| `--to <步骤>` | 到指定步骤结束 |
| `--no-deploy` | 不接入 Jellyfin，只生成文件 |
| `--no-semantic` | 跳过需要语言模型的步骤，用已有判定结果继续 |

步骤名依次为：`index`、`scan`、`identify`、`resolve`、`reresolve`、`apply`、
`recheck`、`split`、`overflow`、`materialize`、`nfo`、`deploy`、`report`。

任何一步失败，流程会停下并打印续跑命令。已完成的判定不会重新调用模型，
用 `--from` 从中断处继续即可。

## 命令参考

### 全局选项

| 选项 | 说明 |
|---|---|
| `--config FILE` | 指定配置文件，默认查找 `./animediator.toml` |
| `--work-dir DIR` | 覆盖中间产物目录 |
| `--vfs DIR` | 覆盖输出目录 |
| `--dry-run` | 不写任何文件，只打印计划 |
| `-y, --yes` | 跳过交互确认 |

全局选项写在子命令前后均可。

### 初始化与诊断

`init [--detect] [--force]`　生成配置模板。`--detect` 先探测本机环境，
`--force` 覆盖已存在的配置。

`config`　打印当前生效的配置及各项来源。

`doctor`　逐项检查目录、磁盘、索引、Jellyfin 与语义层的可用性。退出码非零
表示存在阻塞项。

### 索引维护

`bgm-build [--offline] [--force] [--check] [--dump ZIP] [--out DB]`　建立索引。
默认自动获取最新数据；`--offline` 只用本地数据；`--force` 强制重新下载；
`--check` 只验证已有索引；`--dump` 指定本地文件（指定后不联网）。

`bgm-update [--offline] [--force]`　只更新数据，不重建索引。

`bgm-check`　输出索引的实际查询结果与覆盖率。

`bgm-aodb`　把外部标题库并入作业库，用于提高归库命中率。

`bgm-merge`　把外部标题库的别名并入离线索引。

`bgm-people`　从全量数据解析班底、声优与角色之间的关联。

`bgm-consts [--staffs FILE] [--relations FILE]`　建立职位与关联类型的中文对照表，
用于把 Bangumi 的编号翻译成可读名称。需要 PyYAML。对照表默认从 `data/staffs.yml`
与 `data/relations.yml` 读取。

`meta search <关键词>` / `meta get <bgm_id>`　直接查询单条元数据，用于排查。

### 判定

`scan`　遍历配置中的所有磁盘，记录含视频文件的目录。只读操作。

`identify [--workers N] [--only PATTERN] [--limit N]`　按单元调用语言模型，
产出目录内容的结构化判定。结果逐条追加写入，中断后重跑会跳过已完成的单元。

`resolve`　查询名字索引，把单元对应到 Bangumi 条目。先做本地精确匹配，
未命中的交给模型判定。

`reresolve [--only SUBSTR] [--redo]`　让模型重新查看目录原始路径确认身份。
目录名中常带集号区间（`001-032(221-252)`），是区分同一系列多个条目的主要依据。

`reresolve-search [--only SUBSTR] [--redo]`　在上一版本基础上，允许模型先输出
检索词再查询本地索引，用于关联表覆盖不到的作品。

`apply [--dry-run]`　把复核结果写回作业库。修改前生成 JSON 备份与整库快照，
仅更新 `unit.bgm_id` 一列。

`translate [--limit N]`　为离线索引与模型都无法识别的条目补充中文名与简介。

### 修正

`recheck [--list] [--limit N] [--redo]`　检测有正片容量却没有任何正片归属的
条目，让模型带着该条目的正片集数重新判定。识别阶段缺少集数参照时，容易把
主目录误判为特典目录。

`split [--list] [--limit N] [--redo]`　处理正片数量超出条目容量的单元。
典型情况是一个目录同时包含两季（各 25 集），单一条目无法容纳。

`overflow [--limit N]`　处理拆分后集号仍无法落入条目集号空间的分组。

### 输出

`materialize [--out DIR] [--force]`　生成输出目录。目标目录非空且不包含
`bangumi.ini` 时中止，`--force` 可跳过该检查。

`nfo [--write] [--series]`　写入集级 NFO。不加 `--write` 时只统计不写盘。
`--series` 同时生成 `tvshow.nfo`，供 Kodi、Emby 等读取标准 NFO 的客户端使用。

### Jellyfin

`deploy [--rebuild] [--recreate-container]`　将输出目录接入 Jellyfin 媒体库并触发扫描。
`--rebuild` 在目录结构大幅变化后使用。

接入需要 Jellyfin 容器能读到输出目录。命令会先检查容器是否已经挂载该目录：

- 已挂载 → 跳过容器操作，直接接线。
- 没挂载 → 打印需要补的 `-v` 参数并停下，不改动容器。多数情况下把这一行
  加进自己的 compose 再重启容器更稳妥。
- 容器不存在且配置目录也不存在 → 停下。照原样创建会得到一个空的 Jellyfin。

确实要让本工具重建容器（会删除原容器，只保留 `[jellyfin]` 段里写的镜像、
路径、PUID 等），加 `--recreate-container`。

`jellyfin <action>`　单独执行某一操作：`wire`（接入）、`rebuild`（重建媒体库）、
`grouping`（关闭自动分季）、`images`（调整图片获取顺序）、`serialize`（限制
扫描并发）、`scan`（触发扫描）。

所有写操作在执行前保存改动前的配置快照。加 `--dry-run` 时只打印计划。

### 报告

`report [--no-jellyfin]`　汇总收录情况，输出 `work/web/index.html` 与
`work/动漫库清单.csv`、`.md`。`--no-jellyfin` 跳过与 Jellyfin 的通信。

## 输出目录结构

```
<vfs>/葬送的芙莉莲 (2023)/
├── bangumi.ini
├── Season 01/
│   ├── bangumi.ini
│   ├── 葬送的芙莉莲 - S01E01.mkv      # 符号链接
│   └── 葬送的芙莉莲 - S01E01.nfo
└── Season 00/
    ├── bangumi.ini
    └── 葬送的芙莉莲 - S00E01.mkv
```

`bangumi.ini` 内容：

```ini
[Bangumi]
ID=400602
```

`Season 01` 下的同名 `bangumi.ini` 不可省略。Bangumi 插件的集级提供器读取的是
文件所在目录的配置，只有番剧目录带配置时，集级别的条目 ID 为空，
中文集名不会生效。

## 各客户端的兼容性

Jellyfin 是唯一实测过的客户端，`bangumi.ini` 与 NFO 两条路径都验证过。

Kodi、Emby、Infuse 读取标准 NFO，加 `nfo --series` 后可获得文字元数据，
但不包含海报与图片。

Plex 不读取 NFO，也不识别 `bangumi.ini`，不受支持。

## 实现说明

### 归库为何不用相似度匹配

`resolve` 按可信度顺序尝试候选名称，命中即停止，只用等值比较。
早期版本使用加权评分（可信度×精确度×集数吻合），权重由人工设定，
实测把《葬送的芙莉莲》匹配到《悠悠式》时仍显示 `1.00 high`。
去掉评分后，误配的判定依据从"分数高低"变成"能否在索引中查到"。

### 特典不写集名

Bangumi 全库收录约 150 条特殊章节，而实际收藏中的特典（无字幕片头片尾、
菜单、宣传片）可达数千个，数量相差两个数量级。按顺序对应必然大面积错位，
因此特典只确定集号，集名保留原始文件名。

### 季的划分

Bangumi 的数据模型是扁平的：一个条目对应一条 1..N 的章节序列，不同季是不同的
条目。若按识别阶段给出的季号拆分目录，《火影忍者疾风传》会被拆成三个季，
而它实际是一条连续章节序列。因此输出固定使用 `Season 01` 与 `Season 00`。

### 安全措施

`--dry-run` 对所有写操作生效。

`materialize` 检查目标目录；非空且没有 `bangumi.ini` 时中止。

`apply` 修改数据库前生成备份与快照。

涉及 Jellyfin 配置的写操作在执行前保存快照。

输出目录中若已存在同名真实文件，跳过而不覆盖。

## 故障排查

**Jellyfin 扫描后没有内容**
检查 `media.container` 是否为容器内路径。可以在 Jellyfin 容器内执行
`ls -l /vfs/<番剧目录>/Season\ 01/` 确认链接是否可解析。

**集数对不上或季目录为空**
运行 `animediator report` 查看待处理列表。常见原因是本地集数与 Bangumi 条目
不一致（收藏本身不全），或该目录在识别阶段被判为特典目录，可用
`recheck --list` 确认。

**报错 unknown series 或缺少中文名**
确认 `Season 01/bangumi.ini` 存在。Bangumi 插件的集级提供器读取文件所在
目录的配置。

**依赖语言模型的命令报错**
`semantic.backend` 为 `none`。运行 `animediator doctor` 查看所需配置项。

**模型返回空内容**
使用推理模型时未设 `disable_thinking`。

**索引查询全部落空**
`bgm.db` 未建立或路径不对。运行 `animediator bgm-check` 验证。

## 已知限制

语言模型对超过 60 个文件的目录处理较慢，可能出现重试。

`bgm.db` 来自每周发布的归档数据，新番可能尚未收录。

一个目录中若混有分属不同系列的作品，归库只能选择一个条目，另一部分会
显示为正片缺失。

特典没有中文集名。

## 目录说明

```
src/animediator/
├── config.py            配置装配与路径解析
├── providers.py         语义层客户端
├── textutil.py          文本归一化
├── db.py                作业库结构
├── bgm/                 离线索引的建立与查询
├── scan.py units.py identify.py   扫描与判定
├── resolve.py reresolve*.py apply.py   归库
├── recheck.py split.py overflow.py     修正
├── materialize.py nfo.py               输出
├── jellyfin.py deploy.py               客户端对接
├── report.py                           报告
└── run.py cli.py wizard.py             入口
```

`tests/test_smoke.py` 无需额外依赖即可运行：

```bash
python3 tests/test_smoke.py
```

## 许可

MIT
