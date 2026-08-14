"""LLMClient —— 统一的大模型调用客户端（Phase 0 完成版，逐行教学注释）

这个文件是整个项目被调用次数最多的基础设施，之后每个 agent 都通过它说话。
读这份代码时重点理解四件事：
  1. 配置（LLMConfig）和 行为（LLMClient）为什么要分开
  2. classmethod 作为"另一种构造方式"的用法
  3. 重试 + 指数退避的标准写法
  4. token 统计为什么要放在这里（而不是散落在各个 agent 里）

用法：
    from cra.llm.client import LLMClient
    client = LLMClient.from_config("config.yaml", profile="local_vllm")
    reply = client.chat([{"role": "user", "content": "你好"}])
    print(reply, client.total_tokens_used, client.total_requests)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
import httpx


# OpenAI SDK 的 httpx 客户端在某些 Windows 环境下会挂起（TLS/代理兼容问题），
# 改用 httpx 直接发请求，行为完全等价但更可控。
class APIError(Exception):
    """兼容旧代码的异常类（原来是 openai.APIError）。"""


class RateLimitError(APIError):
    """速率限制（HTTP 429）。Pipeline 捕获它后等 60s 重试。"""


class _RateGate:
    """匀速闸门：按最小间隔放行请求（线程安全）。

    为什么需要它：账号 RPM 配额低时，8 个并行块齐发请求 -> 全撞 429
    -> 全体指数退避，退避期间吞吐为零，总耗时反而更慢。
    主动按 60/rpm 秒的间隔匀速放行，就永远不撞配额墙，
    把"重试等待"的浪费变成"并行在飞"的有效吞吐。
    """

    def __init__(self, interval_sec: float) -> None:
        self.interval = interval_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞到距离上次放行已过 interval 秒，然后占用本次放行名额。"""
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._last + self.interval - now
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(wait)


# ---------------------------------------------------------------------------
# 配置层：一个 dataclass 对应 config.yaml 里 profiles 下的一项。
# 用 dataclass 而不是裸 dict 的好处：字段名写错会在启动时立刻报错，
# 而不是跑到半夜才发现 temperature 拼成了 temperatrue 且静默失效。
# ---------------------------------------------------------------------------
@dataclass
class LLMConfig:
    """一个模型 profile 的完整配置。

    带默认值的字段意味着 yaml 里可以不写，缺省时用这里的默认值。
    """

    base_url: str               # API 地址，本地 vLLM 是 http://localhost:8000/v1
    api_key: str                # 密钥；vLLM 不校验，但 openai SDK 要求非空
    model: str                  # 模型名，必须和服务端加载的名字一致
    temperature: float = 0.2    # 越低越“死板”，审查/代码任务要低
    max_tokens: int = 4096      # 单次回复的最大 token 数
    context_length: int = 128000  # 模型上下文窗口总长（本地 14B 配 8192，云端模型默认 128K）
    timeout: int = 120          # 单次请求超时秒数，14B 本地推理慢，别太短
    # profile 名（如 local_vllm / cloud_api_minimax-m3），from_config 塞进来。
    # 客户端自带身份后，提示词加载器才能"看碟下菜"——按模型定制提示词
    name: str = ""
    # 厂商扩展参数（每家"关思考"的写法都不一样：Qwen3 是 chat_template_kwargs，
    # MiniMax-M3 是 thinking.type——方言收进配置，agent 代码零厂商知识）。
    # 每次请求自动带上；chat() 调用方显式传的 extra_body 优先（可临时覆盖）
    extra_body: dict | None = None
    # 每分钟请求数上限（0 = 不限速）。低配额账号（如 DeepSeek 免费层）
    # 配成 6 左右：客户端主动匀速放行，不撞 429，比被动退避快得多
    rpm: int = 0


class LLMClient:
    """统一的 LLM 调用入口。

    设计要点：所有 agent 只认 LLMClient，不认 OpenAI SDK。
    这样以后想换 SDK、加缓存、加限流，只改这一个文件。
    """

    def __init__(self, config: LLMConfig, max_retries: int = 3) -> None:
        # 把配置整个存下来，chat() 里每个参数都从这里取
        self.config = config
        # 重试次数做成参数：测试里可以传小值加速，线上默认 3 次
        self.max_retries = max_retries
        # 直接用 httpx.Client 替代 OpenAI SDK 的内置客户端：
        # OpenAI SDK 的 httpx 客户端在部分 Windows 环境下 TLS 握手会挂起，
        # 而 httpx 原生客户端完全正常。功能等价，更可控。
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=config.timeout,
        )
        # 统计信息放在客户端里：agent 不用各自记账， Orchestrator 最后来这里取总数
        self._total_tokens = 0      # 累计消耗 token（prompt + completion）
        self._total_requests = 0    # 累计成功请求次数
        # 匀速闸门：rpm>0 时启用，所有并行调用共享同一闸门（client 是单例）
        self._gate = (_RateGate(60.0 / config.rpm) if config.rpm > 0 else None)
    # 拿到合法字段清单 → 从 yaml 字典里筛出认识的键值对 → 拍平成参数造出 LLMConfig → 再用它造出 LLMClient 返回
    @classmethod
    def from_config(cls, path: str | Path, profile: str | None = None) -> "LLMClient":
        """从 YAML 配置文件构造客户端。

        为什么用 @classmethod：它是"第二种构造函数"。
        普通构造：LLMClient(config)           —— 需要你已经有 config 对象
        这种构造：LLMClient.from_config(path)  —— 直接从文件一步建好
        cls 就是类本身（等价于 LLMClient），最后 return cls(cfg) 就是在调 __init__。
        """
        # read_text(encoding="utf-8")：Windows 默认编码是 GBK，不写 utf-8 读中文配置必炸
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

        # 没指定 profile 就用配置文件里的 default_profile
        name = profile or data["default_profile"]
        p = data["profiles"][name]   # 取出对应那一节，是个 dict

        # 只保留 LLMConfig 认识的字段：yaml 里多写的注释性字段不会炸，
        # 少写的字段（如 temperature）则自动用 dataclass 默认值
        # LLMConfig有dataclass装饰器，他会自动给类挂一个 __dataclass_fields__ 属性，它是个字典：字段名 → 字段信息，keys（）就是字段名列表
        keys = LLMConfig.__dataclass_fields__.keys()
        # 只保留 LLMConfig 认识的字段：yaml 里多写的注释性字段不会炸，
        # 少写的字段（如 temperature）则自动用 dataclass 默认值
        # 遍历 p.items()，只保留键在 keys 里的项，构造成新的字典传给 LLMConfig。
        # name=name 把 profile 名一起塞进去——配置自带身份，
        # 提示词加载器（cra.llm.prompts）靠它找模型专版提示词
        cfg = LLMConfig(name=name, **{k: v for k, v in p.items() if k in keys})
        # @classmethod 的最后一步：用 cfg 调用 __init__ 构造实例并返回
        return cls(cfg)

    def chat(self, messages: list[dict], extra_body: dict | None = None, **overrides) -> str:
        """发送一轮对话，返回 assistant 的文本。

        直接用 httpx POST 到 /chat/completions（OpenAI 兼容协议），
        绕过 OpenAI SDK 的 httpx 客户端（在部分 Windows 环境 TLS 会挂起）。

        **overrides 收集多余的关键字参数，例如 chat(msgs, temperature=0.7)
        就变成 overrides = {"temperature": 0.7}，用来临时覆盖配置里的默认值。

        重试逻辑（指数退避）：
        第 1 次失败等 0.5s，第 2 次失败等 1s，第 3 次（最后一次）失败直接抛异常。
        """
        # 构造请求体（OpenAI 兼容格式）
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": overrides.get("temperature", self.config.temperature),
            "max_tokens": overrides.get("max_tokens", self.config.max_tokens),
        }
        # 厂商扩展字段合并进 payload（如 Qwen3 的 enable_thinking、MiniMax 的 thinking）
        merged_extra = extra_body if extra_body is not None else self.config.extra_body
        if merged_extra:
            payload.update(merged_extra)

        for attempt in range(self.max_retries):
            try:
                # 限速闸门：配额紧时匀速放行，从源头避免 429
                if self._gate is not None:
                    self._gate.acquire()
                resp = self._client.post("/chat/completions", json=payload)
                if resp.status_code == 429:
                    raise RateLimitError(
                        f"HTTP 429: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise APIError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()

                # 统计 token 消耗
                usage = data.get("usage")
                if usage:
                    self._total_tokens += usage.get("total_tokens", 0)
                self._total_requests += 1

                return data["choices"][0]["message"]["content"]

            except (httpx.HTTPError, APIError, KeyError) as e:
                if attempt == self.max_retries - 1:
                    raise APIError(f"请求失败（重试 {self.max_retries} 次）: {e}") from e
                time.sleep(0.5 * 2 ** attempt)

    @property
    def total_tokens_used(self) -> int:
        """累计消耗的 token 数。@property 让它像属性一样用：client.total_tokens_used（不加括号）。"""
        return self._total_tokens

    @property
    def total_requests(self) -> int:
        """累计成功请求次数。"""
        return self._total_requests
