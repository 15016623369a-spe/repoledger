"""异步审批从事件日志投影。只有主线程或审批 CLI 写事件。"""
from __future__ import annotations

from dataclasses import asdict

from .events import Event, EventType
from .sandbox import ApprovalRequest


class ApprovalPaused(Exception):
    """工具在副作用发生前暂停。子 run 暂停时，请求保存在子日志。"""

    def __init__(self, request: ApprovalRequest | None = None):
        super().__init__("等待审批")
        self.request = request


class Approvals:
    def __init__(self, events: list[Event]):
        self.requests: dict[int, Event] = {}
        self.decisions: dict[int, bool] = {}
        self.stopped = False
        for event in events:
            if event.type == EventType.APPROVAL_REQUEST:
                self.requests[event.seq] = event
            elif event.type == EventType.APPROVAL_DECISION:
                seq = event.data["request_seq"]
                if seq in self.requests:
                    self.decisions.setdefault(seq, event.data["approved"] is True)
            elif event.type == EventType.RUN_STOP:
                self.stopped = True

    def pending(self) -> list[Event]:
        if self.stopped:
            return []
        return [event for seq, event in self.requests.items() if seq not in self.decisions]


class JournalApproval:
    """每个工具只读一份日志投影，未获决策时把待办交回调度器。"""

    def __init__(self, approvals: Approvals, call_key: str):
        self.approvals = approvals
        self.call_key = call_key

    def ask(self, request: ApprovalRequest) -> bool:
        fields = asdict(request)
        for seq, event in reversed(self.approvals.requests.items()):
            data = event.data
            same_call = (data["call_key"] == self.call_key
                         and all(data.get(key) == value for key, value in fields.items()))
            same_scope = request.scope and request.scope == data.get("scope")
            if (same_call or same_scope) and seq in self.approvals.decisions:
                return self.approvals.decisions[seq]
        raise ApprovalPaused(request)
