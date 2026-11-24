"""子 agent：长任务的关键——把独立子问题隔离出去，主上下文不被污染。

和"多 agent 协作"不是一回事：这里没有 agent 间对话、没有共享黑板，
只有 **单向扇出 → 单条结构化结论回灌**。这正好符合"不做 multi-agent"的非目标。

隔离体现在三处：
  1. 独立 events.jsonl（独立上下文，子 agent 看不到父的历史）；
  2. 独立预算（子任务烧不穿主任务）；
  3. 只回传一条结论（父上下文只增加 O(1) 的 token）。
"""
from __future__ import annotations

import re
import secrets
from dataclasses import replace
from pathlib import Path

from .context import Budget

SUBAGENT_SYSTEM = """你是一个子 agent，被主 agent 派来完成一个自包含的子任务。

工作方式：
- 你有独立上下文，看不到主 agent 的历史，因此任务描述里的信息就是你的全部起点。
- 用工具实际验证，不要凭印象回答。
- 完成后**必须**调用 finish 工具提交结论，否则任务算失败。
- 结论要求：先给 1-3 句直接答案，再列关键证据（文件路径/行号、命令输出摘要）。
- 不要复述过程，不要写客套话。如果没做成，直接说清卡在哪、缺什么。"""


def new_sub_id() -> str:
    return "sub-" + secrets.token_hex(3)


def sanitize(task: str, *, max_len: int = 4000) -> str:
    clean = re.sub(r"\s+", " ", task).strip()
    return clean[:max_len]


def wrap_task(task: str, *, parent_task: str = "") -> str:
    parts = [sanitize(task)]
    if parent_task:
        parts.append(f"\n[背景] 你服务于这个更大的目标，只负责其中一块：{sanitize(parent_task, max_len=600)}")
    parts.append("\n[要求] 完成后调用 finish(answer=...)。")
    return "".join(parts)


def child_budget(parent: Budget) -> Budget:
    """子 agent 拿小份预算：防止一个子任务把主线预算吃光。"""
    return replace(
        parent,
        max_turns=max(6, parent.max_turns // 3),
        max_tool_calls=max(20, parent.max_tool_calls // 3),
        max_input_tokens=parent.max_input_tokens,
        max_total_tokens=max(50_000, parent.max_total_tokens // 4),
        deadline_s=max(120.0, parent.deadline_s / 2),
    )


def child_run_dir(parent_run_dir: Path, sub_id: str) -> Path:
    d = Path(parent_run_dir) / "subagents" / sub_id
    d.mkdir(parents=True, exist_ok=True)
    return d
