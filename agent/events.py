"""append-only 事件日志 —— 整个系统的唯一事实源。

规则只有三条（抄自 DSH，见 DSH架构对V2的可迁移性评估.md §3.1）：
  1. 只追加、不修改、不删除；
  2. 模型可见的内容必须能从日志重建；
  3. 其它一切（UI、指标、评测）都只是日志的只读投影。

一行一事件：{"v":1, "seq":N, "ts":"...", "turn":K, "type":"...", "data":{...}}
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class EventType:
    """事件类型登记表。拼错类型会直接报错，避免日志悄悄坏掉。"""

    RUN_START = "run/start"              # data: task, system, config, deadline
    USER_INPUT = "user/input"            # data: text
    CONTEXT_SNAPSHOT = "context/snapshot"  # data: facts{} （易变事实，追加快照不改写前文）
    MODEL_REQUEST = "model/request"      # data: header{...}
    MODEL_RESPONSE = "model/response"    # data: text, tool_calls[], usage
    TOOL_CALL = "tool/call"              # data: call_id, name, args
    TOOL_RESULT = "tool/result"          # data: call_id, ok, content, artifact, truncated
    GUARD_DENY = "guard/deny"            # data: call_id, guard, reason
    COMPACT_SUMMARY = "compact/summary"  # data: covers[seq_start,seq_end], text, originals[]
    SUBAGENT_SPAWN = "subagent/spawn"    # data: sub_id, task, depth
    SUBAGENT_RESULT = "subagent/result"  # data: sub_id, ok, result, turns
    NOTICE = "run/notice"                # data: text, level
    ERROR = "run/error"                  # data: where, message
    RUN_STOP = "run/stop"                # data: reason, detail

    ALL = {
        RUN_START, USER_INPUT, CONTEXT_SNAPSHOT, MODEL_REQUEST, MODEL_RESPONSE,
        TOOL_CALL, TOOL_RESULT, GUARD_DENY, COMPACT_SUMMARY, SUBAGENT_SPAWN,
        SUBAGENT_RESULT, NOTICE, ERROR, RUN_STOP,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    turn: int
    type: str
    data: dict[str, Any]
    v: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {"v": self.v, "seq": self.seq, "ts": self.ts, "turn": self.turn,
             "type": self.type, "data": self.data},
            ensure_ascii=False, separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(seq=d["seq"], ts=d["ts"], turn=d.get("turn", 0),
                   type=d["type"], data=d.get("data") or {}, v=d.get("v", SCHEMA_VERSION))


class EventLog:
    """追加写入的事件日志。每次 append 后 flush + fsync，保证崩溃后可续。

    日志损坏的处理原则：**截断到最后一个完整行**并记一条 notice，绝不静默丢数据。
    """

    def __init__(self, path: Path, *, fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._corrupt_lines = 0
        self._load()
        self._fh = open(self.path, "a", encoding="utf-8")

    # ---------- 读取 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    ev = Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError) as exc:
                    # 崩溃可能写下半行；严格按"截断到最后完整行"处理
                    self._corrupt_lines = lineno
                    break
                if ev.seq != len(self._events):
                    self._corrupt_lines = lineno
                    break
                self._events.append(ev)

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    @property
    def next_seq(self) -> int:
        return len(self._events)

    @property
    def last(self) -> Event | None:
        return self._events[-1] if self._events else None

    @property
    def last_turn(self) -> int:
        return self._events[-1].turn if self._events else 0

    # ---------- 写入 ----------
    def append(self, type_: str, data: dict[str, Any] | None = None,
               *, turn: int | None = None) -> Event:
        if type_ not in EventType.ALL:
            raise ValueError(f"未知事件类型: {type_!r}")
        with self._lock:
            ev = Event(
                seq=self.next_seq,
                ts=_now(),
                turn=self.last_turn if turn is None else turn,
                type=type_,
                data=data or {},
            )
            self._fh.write(ev.to_json() + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())
            self._events.append(ev)
            return ev

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 便捷查询 ----------
    def of_type(self, type_: str) -> list[Event]:
        return [e for e in self._events if e.type == type_]

    def replay_from(self, seq: int) -> list[Event]:
        return [e for e in self._events if e.seq >= seq]
