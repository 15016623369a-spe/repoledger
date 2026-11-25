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


class EventLogCorrupted(Exception):
    """日志中间（不是尾部）出现坏行。这不是崩溃能造成的，拒绝自动修复。"""


class EventLog:
    """追加写入的事件日志。每次 append 后 flush + fsync，保证崩溃后可续。

    日志损坏的处理原则：
      · 尾部坏行（崩溃时只写了半行）：把文件**物理截断**到最后一个完整事件，
        被截掉的字节原样存进 events.jsonl.corrupt，并记一条 notice；
      · 中间坏行：不是崩溃能造成的，直接报错，交给人处理，绝不静默丢数据。
    只在内存里跳过坏行是不够的：新事件会接在半行后面，下次加载时一起丢失。
    """

    def __init__(self, path: Path, *, fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._corrupt_lines = 0
        self._discarded_bytes = 0
        self.backup_path = self.path.with_name(self.path.name + ".corrupt")
        self._load()
        self._fh = open(self.path, "a", encoding="utf-8")

    # ---------- 读取 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        pos = 0            # 当前行起点
        good_end = 0       # 最后一个完整事件之后的字节位置
        lineno = 0
        missing_newline = False
        bad_line = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            end = len(data) if nl == -1 else nl + 1
            raw = data[pos:end].rstrip(b"\r\n")
            lineno += 1
            if raw.strip():
                try:
                    ev = Event.from_dict(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
                    bad_line = lineno
                    break
                if ev.seq != len(self._events):
                    bad_line = lineno
                    break
                self._events.append(ev)
                missing_newline = nl == -1   # 完整事件但换行没写进去
            pos = end
            good_end = end

        if bad_line:
            rest = data[pos:]
            if rest.count(b"\n") > 1 or (rest.count(b"\n") == 1 and not rest.endswith(b"\n")):
                raise EventLogCorrupted(
                    f"{self.path} 第 {bad_line} 行损坏，且后面还有内容。"
                    "这不是进程崩溃能造成的（崩溃只会留下最后半行），请人工检查后再续跑。")
            self._corrupt_lines = bad_line
            self._discarded_bytes = len(data) - good_end
            with open(self.backup_path, "ab") as bak:
                bak.write(data[good_end:])
                if not data.endswith(b"\n"):
                    bak.write(b"\n")
            with open(self.path, "r+b") as fh:
                fh.truncate(good_end)
                fh.flush()
                os.fsync(fh.fileno())
        elif missing_newline:
            with open(self.path, "ab") as fh:
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())

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
