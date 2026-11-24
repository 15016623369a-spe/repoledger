"""一个可续跑、可审计的最小 agent harness。

对外只需要这几个名字：
    Harness / HarnessConfig  —— 跑任务
    Session                  —— 读日志、做投影
    derive_messages          —— 日志 → 消息（纯函数）
"""
from .context import Budget, estimate_tokens
from .events import Event, EventLog, EventType
from .llm import LLMError, LLMResponse, MockProvider, OpenAICompatProvider
from .loop import DEFAULT_SYSTEM, Harness, HarnessConfig, RunResult
from .sandbox import ApprovalGate, ApprovalRequest, check_command, resolve_within
from .session import Session, derive_messages, open_run
from .subagent import SUBAGENT_SYSTEM
from .tools import ToolCall, ToolRegistry, ToolSpec, default_registry

__all__ = [
    "Harness", "HarnessConfig", "RunResult", "DEFAULT_SYSTEM",
    "Session", "derive_messages", "open_run", "EventType", "Event", "EventLog",
    "Budget", "estimate_tokens",
    "ToolRegistry", "ToolSpec", "ToolCall", "default_registry",
    "ApprovalGate", "ApprovalRequest", "check_command", "resolve_within",
    "LLMError", "LLMResponse", "MockProvider", "OpenAICompatProvider",
    "SUBAGENT_SYSTEM",
]

__version__ = "0.1.0"
