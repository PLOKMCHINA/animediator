# 迁移规范（打包时给每个模块的同一份约束）

新包在 `/root/animediator-pkg/`，源码 `/root/animediator-pkg/src/animediator/`。
旧工程在 `/root/animediator/`（**只读参考，绝对不要改它**）。

## 一条铁律

**逻辑一行不改。** 这套代码是调试出来的，每个判断后面都有实测。
迁移只允许做下面四件事，其它一律保持原样（包括注释、prompt 原文、
变量名、判断顺序）：

1. **常量 → 配置**：模块顶部的
   `X = os.environ.get("AMI_Y", "硬编码默认值")`
   改成从 `config.get()` 里取（见下表）。硬编码的绝对路径必须消失。
2. **导入路径**：`import llm` → `from .providers import ask, ask_json, require`；
   `import db` → `from .db import ...`；`norm()` 统一从 `.textutil` 导入
   （不要再留第七份拷贝）。
3. **包内相对路径**：`"work/xxx.json"` → `cfg.w("xxx.json")`。
4. **入口签名**：`def main():` → `def main(args=None):`，命令行参数优先取
   `args`（CLI 已经传进来了），没有就退回原来的 `sys.argv` 行为。

**不要"顺手改进"。** 觉得哪里可疑，写进文件末尾的 `# TODO(迁移存疑): ...`
注释里，别自己改。

## 配置 API

```python
from .config import get as get_cfg, FLAGS
cfg = get_cfg()

cfg.work          # Path  work/            中间产物
cfg.data_dir      # Path  data/            dump.zip / bgm.db
cfg.vfs           # Path  虚拟库输出目录
cfg.dump          # Path  dump.zip
cfg.bgm_db        # Path  bgm.db          离线索引
cfg.db            # Path  work/animediator.db
cfg.media         # dict  {"SDA": {"root":..., "container":...}, ...}
cfg.jellyfin      # dict  {"url","api_key","library","vfs_in_container","user_id"}
cfg.semantic      # dict  {"backend","base_url","api_key","model","workers",...}
cfg.proxy         # str

cfg.w("tree.json")     # work/tree.json
cfg.tree_json / cfg.units_json / cfg.brain_jsonl / cfg.recheck_json
cfg.split_json / cfg.overflow_json / cfg.status_json   # 都是现成的属性

FLAGS.dry_run     # True 时不许写外部系统（Jellyfin/虚拟库/NFO），只打印
```

## 环境变量对照（旧名保留兼容，新代码不要再直接用）

| 旧 | 新 |
|---|---|
| `AMI_VFS` | `cfg.vfs` |
| `AMI_BGMDb` | `cfg.bgm_db` |
| `AMI_DUMP` | `cfg.dump` |
| `AMI_WORKERS` | `cfg.semantic["workers"]` |
| `AMI_MODEL` | `cfg.semantic["model"]` |
| `AMI_PROVIDER` | `cfg.semantic["backend"]` |

## 语义层

```python
from .providers import ask, ask_json, require, sem

require()          # 需要模型的地方先调：没配后端就带说明炸掉，不要静默降级
ask_json(prompt)   # 返回 dict 或 None（失败不抛异常，自行决定重试/跳过）
sem().describe()
```

**没有配置模型时不许静默跑成"什么都没识别出来"。** 需要模型才能工作的
命令一律 `require()` 开路。

## 验证（每个模块都必须过）

```bash
cd /root/animediator-pkg
PYTHONPATH=src python3 -c "import animediator.<模块>"      # 能导入
PYTHONPATH=src python3 -m animediator <命令> --help        # CLI 挂上了
```

然后把模块的 `--help` 输出、以及任何你跑过的真实验证输出贴在结论里。
**没跑过的验证不许写成通过。**

## 报告格式

结论里给：改了哪些文件、每个文件的验证命令与真实输出、以及所有
`# TODO(迁移存疑)`。不要复述代码。
