"""上下文装配 + token 计量 + 预算。

§3.2 的落地：请求 = 日志的纯函数。
  assemble(session, budget) 只看 events + config，不做任何隐藏状态累加。
所以两次跑同一个 case，只要日志一致，请求就逐字节一致 —— 可直接 diff。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .session import Session, derive_messages

# ---------------------------------------------------------------- token 计量
# 不引入 tiktoken：它是"准"，但会带来平台依赖和模型版本耦合。
# 这里用可解释的近似口径：CJK 1 字≈1 token，其它 4 字符≈1 token。
# 口径写死在这里，评测时"计费/压力/近似构成"三者可分开算（见 §3.5）。

def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            cjk += 1
    other = len(text) - cjk
    return cjk + other // 4 + 1


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += 4  # role/分隔符的固定开销
        total += estimate_tokens(m.get("content"))
        for call in m.get("tool_calls") or []:
            total += estimate_tokens(str(call.get("function", {}).get("arguments", "")))
            total += 8
    return total


# ---------------------------------------------------------------- 预算
@dataclass
class Budget:
    """一次 run 的硬预算。全部可从日志派生，不依赖内存计数。"""

    max_turns: int = 40              # 模型调用次数上限
    max_tool_calls: int = 120        # 工具调用总数上限
    max_input_tokens: int = 60_000   # 单次请求输入预算
    max_total_tokens: int = 800_000  # 全程 token 预算
    deadline_s: float = 3600.0       # 墙钟时间上限

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Budget":
        b = cls()
        for k in b.__dict__:
            if k in (cfg.get("budget") or {}):
                setattr(b, k, type(getattr(b, k))((cfg["budget"] or {})[k]))
        return b

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def deadline_exceeded(session: Session, budget: Budget) -> bool:
    st = session.start_event()
    if not st:
        return False
    import datetime as _dt
    started = _dt.datetime.fromisoformat(st.ts)
    elapsed = (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()
    return elapsed > budget.deadline_s


# ---------------------------------------------------------------- 装配
COLLAPSE_MARK = "[collapsed: 工具结果已折叠为指针，原始内容见日志 artifacts]"

# 工具结果回灌默认上限（§3.5 第一档：剪枝但保留指针）
DEFAULT_TOOL_PREVIEW = 4000


@dataclass
class Assembled:
    messages: list[dict[str, Any]]
    est_tokens: int
    collapsed: int = 0          # 被折叠的工具消息数
    included_events: int = 0
    over_budget: bool = False   # 折无可折仍然超预算 → 此时才值得动用摘要

    def header(self, *, provider: str, model: str, tool_names: list[str]) -> dict[str, Any]:
        """这次请求到底带了什么 —— 记进日志，方便事后逐字节解释差异。"""
        return {
            "provider": provider,
            "model": model,
            "tools": tool_names,
            "message_count": len(self.messages),
            "est_tokens_in": self.est_tokens,
            "collapsed_tool_messages": self.collapsed,
            "included_events": self.included_events,
            "over_budget": self.over_budget,
        }


def assemble(session: Session, budget: Budget) -> Assembled:
    """纯函数：日志 + 预算 → 请求消息。

    压力处理是**确定性的**：从最旧的 tool 消息开始折叠成指针，
    直到估算 token 落到预算的 80% 以下。不做模型调用，不产生事件。
    """
    events = session.events
    messages = derive_messages(events)
    est = estimate_messages_tokens(messages)
    ceiling = int(budget.max_input_tokens * 0.8)
    collapsed = 0

    if est > ceiling:
        # 从旧到新折叠 tool 消息，一直折到进入预算为止；
        # 只保留最近 2 条不动（它们通常和当前决策直接相关）。
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        reserved = 2
        for idx in tool_indices[:-reserved] if len(tool_indices) > reserved else []:
            if est <= ceiling:
                break
            old = messages[idx]
            before = estimate_tokens(old.get("content"))
            messages[idx] = {
                "role": "tool",
                "tool_call_id": old.get("tool_call_id", ""),
                "content": COLLAPSE_MARK,
            }
            est -= before - estimate_tokens(COLLAPSE_MARK)
            collapsed += 1

    return Assembled(messages=messages, est_tokens=est, collapsed=collapsed,
                     included_events=len(events), over_budget=est > ceiling)
