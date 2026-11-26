"""模型接入层：一个 Provider 协议 + OpenAI 兼容实现 + 可脚本化的 Mock。

只用标准库（urllib），不引入 openai SDK —— harness 的依赖应该尽量少，
换模型不该动架构。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .settings import dotenv_values


class LLMError(Exception):
    """模型调用失败。loop 会把它记成 ERROR 事件并决定是否重试。"""


class SimulatedCrash(BaseException):
    """测试用：模拟进程被杀（不继承 Exception，不会被兜底捕获）。"""


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_event_data(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                 "args": (c.get("function") or {}).get("arguments")}
                for c in self.tool_calls
            ],
            "usage": self.usage,
        }


class Provider(Protocol):
    name: str

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], *,
                 temperature: float = 0.0) -> LLMResponse: ...


# ---------------------------------------------------------------- 协议校验
class ProtocolError(BaseException):
    """harness 拼出的消息序列不符合 Chat Completions 协议。

    这是 harness 自己的 bug，不是模型或工具的错，所以和 SimulatedCrash 一样
    不继承 Exception，不会被工具层、子 agent 层的兜底 except 吞成一条错误文本。
    """


def protocol_errors(messages: list[dict[str, Any]]) -> list[str]:
    """按 OpenAI Chat Completions 的规则检查消息序列，返回发现的问题（空列表 = 合法）。

    · 带 tool_calls 的 assistant 后面必须紧跟它全部调用的 tool 消息；
    · 不能出现孤立的 tool 消息；
    · arguments 必须是 JSON 对象字符串。
    """
    errs: list[str] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [c.get("id") for c in m["tool_calls"]]
            for c in m["tool_calls"]:
                raw = (c.get("function") or {}).get("arguments")
                try:
                    ok = isinstance(json.loads(raw), dict)
                except (TypeError, json.JSONDecodeError):
                    ok = False
                if not ok:
                    errs.append(f"[{i}] arguments 不是 JSON 对象: {str(raw)[:60]}")
            j = i + 1
            seen: list[Any] = []
            while j < len(messages) and messages[j].get("role") == "tool":
                seen.append(messages[j].get("tool_call_id"))
                j += 1
            if sorted(map(str, seen)) != sorted(map(str, ids)):
                errs.append(f"[{i}] tool_calls {ids} 后紧跟的 tool 消息是 {seen}")
            i = j
            continue
        if m.get("role") == "tool":
            errs.append(f"[{i}] 孤立的 tool 消息 {m.get('tool_call_id')}")
        i += 1
    return errs


class ProtocolCheckingProvider:
    """包在任意 Provider 外面，每次请求前先检查消息协议。

    strict=True（测试里用）：发现问题直接抛 ProtocolError；
    strict=False：只记到 violations，照常把请求发出去。
    """

    def __init__(self, inner: Provider, *, strict: bool = True):
        self.inner = inner
        self.strict = strict
        self.violations: list[str] = []

    @property
    def name(self) -> str:
        return self.inner.name

    def __getattr__(self, item: str) -> Any:   # model 等其它属性透传给被包的 provider
        if item == "inner":
            raise AttributeError(item)
        return getattr(self.inner, item)

    def complete(self, messages, tools, *, temperature: float = 0.0) -> LLMResponse:
        errs = protocol_errors(messages)
        if errs:
            self.violations.extend(errs)
            if self.strict:
                raise ProtocolError("；".join(errs[:3]))
        return self.inner.complete(messages, tools, temperature=temperature)


# ---------------------------------------------------------------- OpenAI 兼容
MAX_RETRY_AFTER_S = 60.0


def _backoff_seconds(attempt: int, headers: Any) -> float:
    """服务端给了 Retry-After（秒数）就按它等，封顶 60s；否则指数退避 1/2/4/8s。"""
    value = headers.get("Retry-After") if headers is not None else None
    if value:
        try:
            return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
        except ValueError:
            pass  # HTTP 日期格式的 Retry-After 少见，退回指数退避
    return float(min(2 ** attempt, 8))


class OpenAICompatProvider:
    """任何 OpenAI 兼容端点：OpenAI、DeepSeek、Moonshot、vLLM、Ollama、OpenRouter。

    配置优先级依次为显式参数、进程环境变量、项目 .env 和默认值。

    配置项：
      HARNESS_BASE_URL  (默认 https://api.openai.com/v1)
      HARNESS_API_KEY
      HARNESS_MODEL     (默认 gpt-4o-mini)
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 180.0,
                 max_retries: int = 3):
        config = dotenv_values()
        configured_url = (config.get("HARNESS_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.base_url = (base_url or os.environ.get("HARNESS_BASE_URL") or configured_url).rstrip("/")
        # 切换端点时，仅使用显式传入或环境变量中的密钥。
        file_key = config.get("HARNESS_API_KEY", "") if self.base_url == configured_url else ""
        self.api_key = api_key or os.environ.get("HARNESS_API_KEY") or file_key
        self.model = model or os.environ.get("HARNESS_MODEL") or config.get("HARNESS_MODEL") or "gpt-4o-mini"
        self.timeout = timeout
        self.max_retries = max_retries
        self.name = f"openai-compat:{self.model}"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        last: Exception | None = None
        for attempt in range(self.max_retries):
            final = attempt == self.max_retries - 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:500]
                finally:
                    exc.close()   # 释放连接，否则每次重试都泄漏一个响应对象
                last = LLMError(f"HTTP {exc.code}: {detail}")
                if exc.code in (408, 409, 429) or exc.code >= 500:
                    if not final:   # 最后一次失败后不必再等
                        time.sleep(_backoff_seconds(attempt, exc.headers))
                    continue
                raise last from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = LLMError(f"网络/解析错误: {exc}")
                if not final:
                    time.sleep(_backoff_seconds(attempt, None))
        raise last or LLMError("未知失败")

    def complete(self, messages, tools, *, temperature: float = 0.0) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post(payload)
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"响应格式异常: {str(data)[:300]}") from None
        return LLMResponse(
            text=choice.get("content") or "",
            tool_calls=choice.get("tool_calls") or [],
            usage=data.get("usage") or {},
        )


# ---------------------------------------------------------------- Mock
Policy = Callable[[list[dict[str, Any]], list[dict[str, Any]]], LLMResponse]


class MockProvider:
    """把"模型"变成一个普通函数，便于测试与离线演示。

    policy(messages, tools) -> LLMResponse
    crash_at: 第 N 次调用时抛 SimulatedCrash，用来验证崩溃续跑。
    """

    name = "mock"

    def __init__(self, policy: Policy, *, crash_at: int | None = None):
        self.policy = policy
        self.crash_at = crash_at
        self.calls = 0

    def complete(self, messages, tools, *, temperature: float = 0.0) -> LLMResponse:
        self.calls += 1
        if self.crash_at is not None and self.calls == self.crash_at:
            raise SimulatedCrash(f"模拟进程在第 {self.calls} 次模型调用时被杀")
        return self.policy(messages, tools)


class ScriptedProvider:
    """按脚本逐条返回，脚本用完就让 finish。测试用。"""

    name = "scripted"

    def __init__(self, script: list[LLMResponse], *, crash_at: int | None = None):
        self.script = list(script)
        self.crash_at = crash_at
        self.calls = 0

    def complete(self, messages, tools, *, temperature: float = 0.0) -> LLMResponse:
        self.calls += 1
        if self.crash_at is not None and self.calls == self.crash_at:
            raise SimulatedCrash(f"模拟崩溃 @ call {self.calls}")
        if self.script:
            return self.script.pop(0)
        return LLMResponse(text="脚本已耗尽", tool_calls=[
            {"id": f"finish-{self.calls}", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "脚本结束"})}}])


def tool_call(id_: str, name: str, **args: Any) -> dict[str, Any]:
    """构造 tool_call 的小助手。"""
    return {"id": id_, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
