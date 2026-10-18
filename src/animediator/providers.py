"""语义层 —— 认片名、判正片/特典的**可选外挂**。

════════ 设计约束（这是这套工具能不能给别人用的关键）════════
引擎本体（扫描、归库、生成虚拟库、写 NFO、报告）**零依赖、零模型**。
需要"看懂"的地方才调这里，而后端是可换的：

    backend = "none"      不配模型。依赖语义层的命令会**明确报错并告诉你怎么配**，
                          其余命令照常工作。
    backend = "openai"    任何 OpenAI 兼容的 /chat/completions：
                            本地 llama.cpp / Ollama / vLLM  → 有算力就本地跑
                            云端 API                        → 没算力就接外部
                          引擎侧代码完全一样。

所以「有算力的用户本地跑，没有的接外部 API」不是两套代码，是一行配置。

════════ 为什么自带 HTTP 而不用 requests ════════
`requests` 好用，但它会让整个工程多一个依赖 —— 而模型调用是**可选**
的，不该因为可选功能把必装依赖带进来。标准库 `urllib` 够用。

════════ 失败姿势 ════════
`ask()` 失败返回 `None` 而不抛异常，让上层决定是重试、跳过还是记账。
但**没配后端**是配置错误，必须立刻炸 —— 静默返回 None 会让整条链
在"什么都没识别出来"的状态下跑完，白烧几小时。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .config import get as _cfg

NOT_CONFIGURED = """\
这个命令需要语义层（用模型看懂片名/判断正片特典），但当前没配后端。

两种配法，挑一个：

  ① 本地跑（有显卡/内存够）—— llama.cpp、Ollama、vLLM 都行，它们都提供
     OpenAI 兼容接口。以 Ollama 为例：

        ollama pull qwen2.5:14b

     然后 animediator.toml：

        [semantic]
        backend  = "openai"
        base_url = "http://127.0.0.1:11434/v1"
        model    = "qwen2.5:14b"
        api_key  = "ollama"          # 本地通常随便填

  ② 接云端（没算力）—— 任何 OpenAI 兼容服务：

        [semantic]
        backend  = "openai"
        base_url = "https://api.deepseek.com/v1"
        api_key  = "sk-..."
        model    = "deepseek-chat"

  ⚠️ 如果用的是**推理模型**（名字里带 reason/thinking/r1 之类），
     记得加一行 `disable_thinking = true` —— 不关思考的话 token 全烧在
     推理上，返回的正文是空的，表现出来就是"每次都失败"。

暂时不想配模型的话，可以先跑 `animediator lib rulematch`：
它只用目录名查离线索引，不调模型。实测在 455 个真实目录上覆盖率 79%、
唯一命中时准确率 61% —— 错的地方集中在一个目录含多部作品、以及续作之间
靠名字分不清，这两件纯字符串比对做不到。所以它只给建议，不写库。

要让结果真正可用，还是得配语义层。
"""


def _use_local_proxy(cfg) -> None:
    """把配置里的代理灌进环境变量。

    标准库 urllib 认 `http_proxy`/`https_proxy`；某些环境（国内直连 bgm.tv
    不稳、或需要走机场）必须显式设置。只在配置里写了才动，不覆盖已有的。
    """
    p = cfg.proxy
    if not p:
        return
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.setdefault(k, p)


class Semantic:
    def __init__(self, cfg=None):
        self.cfg = cfg or _cfg()
        self.s = self.cfg.semantic
        self.backend = self.s.get("backend", "none")
        self._warned = False

    # ── 状态 ──
    @property
    def enabled(self) -> bool:
        return self.backend not in ("", "none")

    def require(self) -> None:
        """需要语义层的地方先调这个：没配就带着配置说明炸掉。"""
        if self.enabled:
            return
        raise SystemExit(NOT_CONFIGURED)

    def describe(self) -> str:
        if not self.enabled:
            return "语义层：未配置（依赖它的命令不可用）"
        return "语义层：%s  %s  model=%s" % (
            self.backend, self.s.get("base_url"), self.s.get("model"))

    # ── 端点 ──
    def _endpoint(self):
        base = (self.s.get("base_url") or "").rstrip("/")
        if not base:
            raise SystemExit(
                "语义层配了 backend=%s 但没写 base_url。\n"
                "  在 animediator.toml 的 [semantic] 里补上，例如\n"
                "    base_url = \"http://127.0.0.1:11434/v1\"" % self.backend)
        url = base + "/chat/completions" if not base.endswith("/chat/completions") else base
        key = self.s.get("api_key") or os.environ.get("AMI_KEY") or "not-needed"
        model = self.s.get("model") or ""
        if not model:
            raise SystemExit("语义层没写 model（要调哪个模型）。")
        return url, key, model

    def ask(self, prompt, max_tokens=None, temperature=None, json_mode=True):
        """问一次模型，返回文本；失败返回 None（不抛异常）。"""
        if not self.enabled:
            self.require()
        _use_local_proxy(self.cfg)
        url, key, model = self._endpoint()
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or self.s.get("max_tokens", 16000),
            "temperature": (self.s.get("temperature", 0.1)
                            if temperature is None else temperature),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.s.get("disable_thinking"):
            # 推理模型的开关。各家叫法不一，这里给最常见的两种写法，
            # 不认识的多余字段服务端一般会忽略。
            body["thinking"] = {"type": "disabled"}
        data = json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + key,
                   "Content-Type": "application/json",
                   "User-Agent": self.cfg.bgm.get("user_agent", "animediator")}

        retries = int(self.s.get("retries", 10))
        timeout = int(self.s.get("timeout", 240))
        last = ""
        for k in range(retries):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode()
                d = json.loads(raw)
                # ⚠️ 别用 d["choices"][0]["message"]["content"] 硬取 ——
                #    网关偶发返回别的结构（内容审查、上游报错、空 choices），
                #    硬取就 KeyError，被当成"重试 N 次然后失败"，
                #    浪费配额还看不出原因。
                try:
                    choice = d["choices"][0]
                    out = (choice.get("message", {}).get("content")
                           or choice.get("delta", {}).get("content") or "").strip()
                    fin = choice.get("finish_reason")
                except (KeyError, IndexError, TypeError):
                    last = "结构异常:%s" % raw[:120].replace("\n", " ")
                    time.sleep(1.5)
                    continue
                if out:
                    if fin == "length":
                        # 截断的 JSON 解不出来 —— 记一笔，让上层知道是长度问题
                        print("   [语义层] 输出被 max_tokens 截断（%d 字）" % len(out))
                    return out
                last = "空正文(finish=%s)" % fin
                if fin == "length" and not self.s.get("disable_thinking"):
                    if not self._warned:
                        self._warned = True
                        print("   [语义层] 反复返回空正文且 finish=length —— "
                              "如果用的是推理模型，在配置里加 disable_thinking = true")
                time.sleep(1.0)
            except urllib.error.HTTPError as ex:
                try:
                    code = json.loads(ex.read().decode())["error"]["code"]
                except Exception:
                    code = ""
                last = "HTTP %s code=%s" % (ex.code, code)
                if str(code) == "1113":            # 欠费，重试无意义
                    print("   [语义层] 该模型余额不足/无资源包，停止重试")
                    return None
                if ex.code == 429 or str(code) in ("1302", "1305"):
                    # 过载/限流：指数退避 + 抖动
                    time.sleep(min(45.0, 2.0 * (1.7 ** k)) + (k % 3) * 0.9)
                    continue
                time.sleep(1.5 * (k + 1))
            except Exception as ex:
                last = type(ex).__name__
                time.sleep(1.5 * (k + 1))
        print("   [语义层失败] %s（%d 次重试）" % (last, retries))
        return None

    def ask_json(self, prompt, **kw):
        txt = self.ask(prompt, **kw)
        if not txt:
            return None
        t = txt.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[-1]
            if t.endswith("```"):
                t = t[:-3]
            t = t.strip()
        obj = extract_json(t)
        if obj is None:
            print("   [语义层 JSON 解析失败] %r" % txt[:200])
        return obj


def extract_json(t):
    """从模型输出里抠出真正的 JSON 对象。

    ★ 实测坑一：模型会把 `response_format` 的设定**回声**在正文开头
      （`{"type": "json_object"}\\n{"title": ...}`）。
      用「第一个 { 到最后一个 }」去截会得到含两个对象的非法串。
      做法：从每个 `{` 起做括号配对扫描（跳过字符串内括号），
      只接受配对完整的片段，把能解析的对象**都收集起来**。

    ★ 实测坑二（严重）：这里原来写过一个硬编码的**字段白名单**
      （`"title" in obj or "files" in obj`）。结果 `{"pick":2}`、
      `{"picks":{...}}` 这类完全合法的输出全被判为"解析失败"，
      上层只好重试，白白烧配额还引发限流。
      **判断该归模型，不该由我的字段名单替它否决。**
      现在只排除 API 回声那一个已知的非内容对象（精确等值匹配），
      其余一律原样返回。
    """
    n = len(t)
    found = []
    i = 0
    while i < n:
        if t[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        end = None
        for j in range(i, n):
            c = t[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            break
        frag = t[i:end + 1]
        try:
            found.append(json.loads(frag))
        except Exception:
            pass
        i = end + 1

    real = [o for o in found
            if not (isinstance(o, dict) and len(o) == 1
                    and o.get("type") == "json_object")]
    if real:
        return real[0]
    return found[0] if found else None


# ── 模块级便捷入口：`from .providers import ask, ask_json` ──
_DEFAULT: Semantic | None = None


def sem() -> Semantic:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Semantic()
    return _DEFAULT


def ask(prompt, **kw):
    return sem().ask(prompt, **kw)


def ask_json(prompt, **kw):
    return sem().ask_json(prompt, **kw)


def require():
    sem().require()
