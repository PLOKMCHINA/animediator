# AniMediator

把命名混乱的动漫目录整理成媒体服务器能直接读取的媒体库。

原始文件不移动、不重命名。输出是一棵只含符号链接的目录树，随时可以删掉重建。

## 识别方式与常见刮削器的区别

常见做法是拿文件名去数据库里匹配。发布组命名千奇百怪，一个目录里混着正片、
OVA、特典、菜单，续作与前作共用一个文件夹时，文件名匹配就会出错。

这里把识别拆成两件独立的事：

- **看懂目录**：由语言模型判断这个目录里有几集正片、哪些是特典、可能是什么作品。
  它不需要知道准确的译名，只需要读懂发布组的命名习惯。
- **查证作品**：作品与 Bangumi 条目的对应关系，用本地离线索引做等值查询，
  不依赖模型记忆，也不联网。

模型只负责它擅长的部分，查证交给可以复现的索引。这样即使模型说错译名，
最终写入库的条目 ID 仍然是查证过的。

**语义层决定这套工具能用到什么程度。** 它负责读懂目录：哪些文件是正片、哪些是
特典、集号怎么排、一个目录里是不是塞了两季。这部分没有等价替代品。实测拿目录名
硬查离线索引（不调模型），在 455 个真实目录上覆盖率 79%，唯一命中的准确率只有
61%——错的恰好是最难的两种：一个目录含多部作品、续作之间靠名字区分不开。

没配模型时可以先跑 `lib rulematch` 看能匹配到什么程度，它只给候选、不写库。

模型后端可换：本地 Ollama、vLLM，或任何 OpenAI 兼容的云端接口，换后端不改代码。

## 工作方式

**判定**：扫描磁盘，得到目录与文件的清单；对每个目录产出归属判定——属于哪部作品、
哪些文件是正片、集号怎么对应。

**生成**：按判定结果构造输出目录。每个 Bangumi 条目一个目录，下设 `Season 01`
（正片）与 `Season 00`（特典），写入 `bangumi.ini` 与集级 NFO。

集号取自条目的章节序号，不与文件名编号直接对应——发布方常用局部编号
（`001-032`）或部番编号（`113-143`），两者都会按目录名标注的区间换算。

## 安装

**Docker**（推荐，不用管 Python 环境）

```bash
git clone https://github.com/PLOKMCHINA/animediator.git
cd animediator
# 改 docker-compose.yml 里最后一条 volumes，指向你的动漫目录
docker compose run --rm animediator init
```

**直接运行**（需要 Python 3.10+，无第三方依赖）

```bash
git clone https://github.com/PLOKMCHINA/animediator.git
cd animediator
./animediator init
```

仓库根目录的 `./animediator` 只是把 `src/` 加进 `PYTHONPATH` 后调用 Python，
不做任何安装。

**装成命令**

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

构建隔离需要联网拉 `setuptools` 和 `wheel`。国内访问 pypi.org 常不通，用镜像源。
Debian 12 之后的系统 Python 受 PEP 668 保护，直接 `pip install` 会被拒绝，
这是上面用虚拟环境的原因。

## 快速开始

```bash
animediator init        # 问答式配置，探测磁盘、模型、媒体服务器
animediator bgm-build   # 下载离线索引（约 440 MB，只需一次）
animediator doctor      # 检查还缺什么
animediator run         # 跑完整条链
```

`init` 会先探测这台机器：已挂载的磁盘、Docker 容器里的挂载路径、本机跑着的
Ollama 或 vLLM、Jellyfin 服务。探测到的值直接作为默认值，一路回车也能配好。

每一步都能单独重跑，中间产物在 `work/`：

```bash
animediator lib scan            # 只扫盘
animediator run --from resolve  # 从归库接着跑
animediator run --no-semantic   # 没有模型时跳过着色步骤
```

## 配置

`animediator.toml` 是唯一放环境相关信息的地方，代码里没有机器专有的路径。
相对路径以这个文件所在目录为基准。

最少要改三处：

```toml
[media.DISK1]
root      = "/mnt/anime"      # 宿主上的路径，扫描从这里开始
container = "/media/anime"    # 容器里的路径，符号链接指向这里

[jellyfin]
url     = "http://127.0.0.1:8096"
api_key = "..."               # 控制台 → 高级 → API 密钥

[semantic]
backend  = "openai"
base_url = "http://127.0.0.1:11434/v1"   # 本地 Ollama
model    = "qwen2.5:14b"
api_key  = "ollama"
```

`container` 填错不会报错，症状是媒体库扫描后为空或集数异常——链接在容器里解析不到。

盘数不限：有几块盘就写几个 `[media.XXX]` 段，段名随意，扫盘时按段名分发。
扫盘的并行按盘来算——一块盘一个线程，盘内顺序遍历（机械盘上多线程只会互相
打断寻道）。盘内并发只对固态盘开放，按 `/sys/dev/block` 里的 rotational 标记
判断，判断不了就当机械盘处理。`scan_workers`（默认 4）是总并发上限。

用推理模型（名字带 reason / thinking / r1）时要加 `disable_thinking = true`，
否则 token 全花在推理上，返回正文是空的，表现为每次都失败。

## 命令

命令按用途分组，写 `animediator <组> --help` 看组内说明。旧的一级命令名
（如 `animediator scan`）仍然可用。

### 配置与诊断

| 命令 | 作用 |
|---|---|
| `init [--detect] [--force] [--no-wizard]` | 生成配置。`--no-wizard` 不提问，直接出模板 |
| `config` | 打印当前生效的配置及各项来源 |
| `doctor` | 检查目录、磁盘、索引、媒体服务器、语义层 |
| `status` | 看这批数据走到流程哪一步了，下一步该跑什么 |

### 整理

| 命令 | 作用 |
|---|---|
| `lib scan` | 扫盘，产出文件清单。只读 |
| `lib identify` | 对每个目录做归属判定（需要语义层） |
| `lib resolve` | 把文件对应到 Bangumi 条目 |
| `lib rulematch` | 不用模型，只用目录名查索引，给出候选供参考 |
| `lib assign` | 人工指定某个目录对应哪部作品（认错了用它改） |
| `lib undo` | 从整库快照或 bgm_id 备份回滚 |
| `lib verify` | 检查虚拟库里的链接是否还指向存在的源文件 |
| `lib recheck` | 找出被误判成特典的正片 |
| `lib split` | 拆开装了两季的目录 |
| `lib overflow` | 处理集号溢出的情况 |
| `lib materialize` | 生成虚拟库 |
| `lib nfo [--write] [--series]` | 写集级 NFO |
| `run [--from 步骤] [--to 步骤] [--no-deploy] [--no-semantic]` | 按顺序跑完整条链 |

### 索引

| 命令 | 作用 |
|---|---|
| `index bgm-build [--offline] [--force]` | 建立离线索引，默认自动下载最新数据 |
| `index bgm-update` | 只更新数据，不重建索引 |
| `index meta search <词>` / `meta get <id>` | 直接查一条元数据，排查用 |

### 上线

| 命令 | 作用 |
|---|---|
| `serve deploy [--rebuild]` | 把虚拟库接进媒体服务器并扫描 |
| `serve jellyfin <动作>` | 单独执行某项操作：wire / rebuild / grouping / images / serialize / scan |
| `serve report [--no-jellyfin]` | 生成收录情况、HTML 报告与清单 |

所有写操作在执行前保存改动前的快照。加 `--dry-run` 只打印计划。

## 故障排查

**扫描后媒体库是空的**
`[media.X] container` 填成了宿主路径。它必须是媒体服务器容器内看到的路径。

**集数对不上，或 Season 目录是空的**
确认 `Season 01` 下有 `bangumi.ini`。集级提供器读的是文件所在目录的配置，
只有番剧目录带配置时，集级的条目 ID 为空，中文集名不生效。

**报错 unknown series 或没有中文名**
媒体服务器没装 Bangumi 插件，或插件里没配 API。本工具只保证条目 ID 正确，
中文名与海报由插件去取。

**需要模型的命令全部失败**
`semantic.backend` 还是 `none`，或者 `base_url` / `model` 填错。
先跑 `animediator doctor`。

**模型返回空内容**
用的是推理模型但没关思考。加 `disable_thinking = true`。

**索引查询全部落空**
`data/bgm.db` 没建或没下完。跑 `animediator bgm-build`，它会断点续传。

## 客户端兼容性

Jellyfin 是唯一实测过的客户端，`bangumi.ini` 与 NFO 两条路径都验证过。

Kodi、Emby、Infuse 读取标准 NFO，加 `nfo --series` 后可获得文字元数据，
但不包含海报与图片。

Plex 不读取 NFO，也不识别 `bangumi.ini`，不受支持。

## 已知限制

语言模型对超过 60 个文件的目录处理较慢，可能出现重试。

`bgm.db` 来自每周发布的归档数据，新番可能尚未收录。

一个目录里若混有分属不同系列的作品，归库只能选择一个条目，另一部分会显示为
正片缺失。

特典没有中文集名。

## 开发

```bash
python3 tests/test_smoke.py    # 零依赖冒烟测试
```

`src/animediator/` 的模块职责见源码顶部说明。`core/` 是公共基础设施（退出码、
输出器、错误类型、运行上下文），其余模块按流水线命名。

## 许可

MIT
