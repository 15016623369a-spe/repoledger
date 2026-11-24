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


# ---------------------------------------------------------------- OpenAI 兼容
class OpenAICompatProvider:
    """任何 OpenAI 兼容端点：OpenAI、DeepSeek、Moonshot、vLLM、Ollama、OpenRouter。

    环境变量：
      HARNESS_BASE_URL  (默认 https://api.openai.com/v1)
      HARNESS_API_KEY
      HARNESS_MODEL     (默认 gpt-4o-mini)
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 180.0,
                 max_retries: int = 3):
        self.base_url = (base_url or os.environ.get("HARNESS_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("HARNESS_API_KEY") or ""
        self.model = model or os.environ.get("HARNESS_MODEL") or "gpt-4o-mini"
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
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                last = LLMError(f"HTTP {exc.code}: {detail}")
                if exc.code in (408, 409, 429) or exc.code >= 500:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = LLMError(f"网络/解析错误: {exc}")
                time.sleep(min(2 ** attempt, 8))
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
