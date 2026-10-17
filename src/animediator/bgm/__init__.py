"""离线 bgm 索引 —— 整套工具"可靠地知道这是什么番"的地基。

`api.bgm.tv` 挂在 Cloudflare 后面，国内可达性是飘的；而番组计划每周放全量 dump。
把这个 dump 建成一个本地 SQLite，之后所有身份查询都是毫秒级的等值比较，
不再看网络脸色。

    index.py     dump.zip → bgm.db（主体：作品 / 别名 / 剧集 / 人物）
    merge.py     把 aodb 的罗马音与别名并进 sname，提高召回
    people.py    班底 / 声优 / 角色 / 关联
    consts.py    职位号与关联类型的中文对照表（来自 bangumi/common）
    meta.py      对外查询接口：**离线优先，联网兜底**
    check.py     自检：这个索引到底答不答得上真问题
"""
