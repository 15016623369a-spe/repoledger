"""Session = 事件日志 + 纯函数投影。

核心不变量：**模型可见的每一条消息，都能从 events.jsonl 重建**。
没有任何状态存在内存里，重启后 derive 出来的一模一样。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import Event, EventLog, EventType

SNAPSHOT_PREFIX = "[context snapshot]"
NOTICE_PREFIX = "[harness]"
SUMMARY_PREFIX = "[compacted history]"


def _shadowed_seqs(events: list[Event]) -> set[int]:
    """被压缩摘要覆盖的事件 seq 集合。压缩只标记、原始记录永不删除。"""
    shadowed: set[int] = set()
    for ev in events:
        if ev.type != EventType.COMPACT_SUMMARY:
            continue
        covers = ev.data.get("covers") or []
        if len(covers) == 2:
            shadowed.update(range(int(covers[0]), int(covers[1]) + 1))
    return shadowed


def _summary_message(ev: Event) -> dict[str, Any]:
    covers = ev.data.get("covers") or [0, 0]
    return {
        "role": "user",
        "content": (f"{SUMMARY_PREFIX} 以下是 seq {covers[0]}..{covers[1]} 的折叠摘要，"
                    f"原始事件仍在日志中可查：\n{ev.data.get('text', '')}"),
    }


def _tool_call_arguments(args: Any) -> str:
    """把日志里的工具参数还原成 API 要求的 arguments 字符串。

    MODEL_RESPONSE 里存的是模型原样产出的 JSON 字符串：合法就原样回放（不能再
    json.dumps 一次，否则模型看到的是二次编码的字符串）；非法或不是对象时用 "{}"
    占位，错误原因和原始参数已经写在对应的 tool 结果里。
    """
    if isinstance(args, str):
        if not args.strip():
            return "{}"
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return "{}"
        return args if isinstance(parsed, dict) else "{}"
    return json.dumps(args or {}, ensure_ascii=False)


def derive_messages(events: list[Event]) -> list[dict[str, Any]]:
    """把事件日志投影成 LLM 消息序列。纯函数：同样输入永远同样输出。

    系统提示词是 run/start 里的一个节点，不是独立参数（§3.2）；
    易变事实一律走 context/snapshot 追加，绝不回写 system（§3.3）。

    协议约束：带 tool_calls 的 assistant 消息后面必须紧跟它的全部 tool 消息。
    续跑时插入的 notice 等非 tool 消息如果落在两者之间，会延后到这一轮的
    tool 结果补齐之后再投影，否则真实 API 会直接拒绝请求。
    """
    shadowed = _shadowed_seqs(events)
    messages: list[dict[str, Any]] = []
    open_calls: set[str] = set()          # 最近一条 assistant 发出、还没有结果的 call_id
    deferred: list[dict[str, Any]] = []   # 这期间出现的非 tool 消息

    def emit(msg: dict[str, Any]) -> None:
        (deferred if open_calls else messages).append(msg)

    def flush() -> None:
        messages.extend(deferred)
        deferred.clear()

    # 仍生效的摘要投影到它覆盖区间的起点，而不是它自己写入日志的位置：
    # 摘要讲的是更早的历史，排在保留下来的最近几轮后面会让时间线倒过来。
    summary_at: dict[int, Event] = {}
    for ev in events:
        if ev.type == EventType.COMPACT_SUMMARY and ev.seq not in shadowed:
            covers = ev.data.get("covers") or []
            if len(covers) == 2 and int(covers[0]) < ev.seq:
                summary_at[int(covers[0])] = ev
    placed = {ev.seq for ev in summary_at.values()}

    for ev in events:
        if ev.seq in summary_at:
            emit(_summary_message(summary_at[ev.seq]))
        if ev.seq in shadowed or ev.seq in placed:
            continue

        if ev.type == EventType.RUN_START:
            system = ev.data.get("system") or ""
            if system:
                emit({"role": "system", "content": system})

        elif ev.type == EventType.USER_INPUT:
            emit({"role": "user", "content": ev.data.get("text", "")})

        elif ev.type == EventType.CONTEXT_SNAPSHOT:
            facts = ev.data.get("facts") or {}
            body = "\n".join(f"- {k}: {v}" for k, v in facts.items())
            emit({
                "role": "user",
                "content": f"{SNAPSHOT_PREFIX} 本轮生效的事实快照（来源: seq={ev.seq}）\n{body}",
            })

        elif ev.type == EventType.MODEL_RESPONSE:
            # 上一轮的调用如果始终没补齐结果，日志本身就不完整；尽力保持顺序
            open_calls.clear()
            flush()
            text = ev.data.get("text") or ""
            calls = ev.data.get("tool_calls") or []
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"],
                                     "arguments": _tool_call_arguments(c.get("args"))},
                    }
                    for c in calls
                ]
                open_calls.update(c["id"] for c in calls)
            messages.append(msg)

        elif ev.type == EventType.TOOL_RESULT:
            content = ev.data.get("content") or ""
            if ev.data.get("truncated"):
                content += (f"\n[truncated: 完整结果见 {ev.data.get('artifact')} "
                            f"(seq={ev.seq})]")
            call_id = ev.data.get("call_id", "")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": content or "(empty)",
            })
            if call_id in open_calls:
                open_calls.discard(call_id)
                if not open_calls:
                    flush()

        elif ev.type == EventType.COMPACT_SUMMARY:
            # covers 不合法、没能前移的摘要，退回到它自己的位置
            emit(_summary_message(ev))

        elif ev.type == EventType.NOTICE:
            # 不占用 system 前缀，保证 KV cache 前缀稳定
            emit({"role": "user",
                  "content": f"{NOTICE_PREFIX} {ev.data.get('text', '')}"})

    flush()
    return messages


@dataclass
class RunStats:
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    guard_denials: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    compactions: int = 0
    subagents: int = 0
    stopped: bool = False
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Session:
    """一次 run 的完整视图。所有写入只经过 EventLog.append。"""

    def __init__(self, run_dir: Path, *, fsync: bool = True):
        self.run_dir = Path(run_dir)
        self.artifacts_dir = self.run_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(self.run_dir / "events.jsonl", fsync=fsync)
        if self.log._corrupt_lines:  # 崩溃留下的半行，已物理截断并备份
            self.log.append(EventType.NOTICE, {
                "level": "warn",
                "text": (f"日志末尾检测到不完整记录（第 {self.log._corrupt_lines} 行，"
                         f"{self.log._discarded_bytes} 字节），已截断至最后完整事件；"
                         f"被截掉的内容保存在 {self.log.backup_path.name}"),
            })

    # ---------- 写入 ----------
    def append(self, type_: str, data: dict[str, Any] | None = None, *,
               turn: int | None = None) -> Event:
        return self.log.append(type_, data, turn=turn)

    def write_artifact(self, seq_hint: int, content: str, suffix: str = "txt") -> str:
        """把工具完整结果落盘，日志里只存预览 + 指针。"""
        name = f"{seq_hint:05d}{'-' + suffix if suffix else ''}.txt"
        path = self.artifacts_dir / f"{seq_hint:05d}.txt"
        path.write_text(content, encoding="utf-8")
        return str(path.relative_to(self.run_dir))

    # ---------- 投影 ----------
    @property
    def events(self) -> list[Event]:
        return self.log.events

    def messages(self) -> list[dict[str, Any]]:
        return derive_messages(self.log.events)

    def start_event(self) -> Event | None:
        starts = self.log.of_type(EventType.RUN_START)
        return starts[0] if starts else None

    def config(self) -> dict[str, Any]:
        st = self.start_event()
        return (st.data.get("config") or {}) if st else {}

    def task(self) -> str:
        st = self.start_event()
        return (st.data.get("task") or "") if st else ""

    def stats(self) -> RunStats:
        s = RunStats()
        for ev in self.log.events:
            if ev.type == EventType.MODEL_RESPONSE:
                s.turns += 1
                usage = ev.data.get("usage") or {}
                s.tokens_in += int(usage.get("prompt_tokens") or 0)
                s.tokens_out += int(usage.get("completion_tokens") or 0)
            elif ev.type == EventType.TOOL_RESULT:
                s.tool_calls += 1
                if not ev.data.get("ok", True):
                    s.tool_errors += 1
            elif ev.type == EventType.GUARD_DENY:
                s.guard_denials += 1
            elif ev.type == EventType.COMPACT_SUMMARY:
                s.compactions += 1
            elif ev.type == EventType.SUBAGENT_RESULT:
                s.subagents += 1
            elif ev.type == EventType.RUN_STOP:
                s.stopped = True
                s.stop_reason = ev.data.get("reason")
        return s

    def final_answer(self) -> str | None:
        for ev in reversed(self.log.events):
            if ev.type == EventType.RUN_STOP:
                return ev.data.get("detail")
            if ev.type == EventType.MODEL_RESPONSE and ev.data.get("text"):
                # 反向找第一条非空文本会拿到中间思考，这里只在 stop 里取
                continue
        return None

    def close(self) -> None:
        self.log.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_run(root: Path, run_id: str) -> Session:
    return Session(Path(root) / run_id)
