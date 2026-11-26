"""Agent 主循环：turn/step 状态机 + 预算 + 断点续跑。

最重要的设计：**主循环没有内存态**。
每一次迭代都重新从日志推导"下一步该干嘛"（_next_action），
所以进程被杀在任意一步，重开日志继续跑，行为和没崩过一样。
"""
from __future__ import annotations

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .approval import ApprovalPaused, Approvals, JournalApproval
from .compact import CompactPolicy, compact_if_needed
from .context import (Assembled, Budget, assemble, deadline_exceeded,
                      estimate_messages_tokens)
from .events import Event, EventType
from .llm import LLMError, Provider, SimulatedCrash
from .sandbox import ApprovalGate
from .session import RunStats, Session
from .skills import skill_system
from .subagent import (SUBAGENT_SYSTEM, child_budget, child_run_dir, new_sub_id,
                       sub_id_for_call, wrap_task)
from .tools import (DEFAULT_PREVIEW_CHARS, Outcome, ToolCall, ToolContext,
                    ToolError, ToolRegistry, default_registry)

DEFAULT_SYSTEM = """你是一个长任务自动化 agent，通过工具循环推进目标。

原则：
1. 先探明再动手。用 list_dir / read_file / grep 确认现状，不要假设。
2. 每次只做一件可验证的小事，然后用工具验证结果，不要承诺"下一步会做"。
3. 工具报错时读错误信息并修正参数，不要重复同样的失败调用。
4. 大任务用 spawn_subagent 拆成自包含子任务，每个子任务描述要能让别人独立完成。
5. 上下文有限：长输出只摘取需要的片段，不要把整个文件贴回来。被截断的结果用 read_artifact 按需搜索或分段读取。
6. 全部完成后调用 finish 提交结论。finish 是唯一的正常结束方式。

不要输出寒暄、不要复述工具输出、不要解释你将要做什么——直接做。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


@dataclass
class HarnessConfig:
    workspace: Path
    runs_root: Path
    system: str = DEFAULT_SYSTEM
    budget: Budget = field(default_factory=Budget)
    temperature: float = 0.0
    allow_bash: bool = True
    max_subagent_depth: int = 1
    preview_chars: int = DEFAULT_PREVIEW_CHARS
    compact_policy: CompactPolicy = field(default_factory=CompactPolicy)
    # 同一批并行执行的工具调用数上限
    max_parallel: int = 4
    # 写进 run/start 的附加信息（如接入了哪些 MCP Server、注册了哪些工具），便于审计
    extra: dict[str, Any] = field(default_factory=dict)
    async_approval: bool = False

    def to_dict(self, provider_name: str, depth: int) -> dict[str, Any]:
        out = {
            "workspace": str(self.workspace),
            "budget": self.budget.as_dict(),
            "temperature": self.temperature,
            "allow_bash": self.allow_bash,
            "max_subagent_depth": self.max_subagent_depth,
            "max_parallel": self.max_parallel,
            "provider": provider_name,
            "async_approval": self.async_approval,
            "depth": depth,
        }
        out.update(self.extra)
        return out


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    reason: str
    answer: str | None
    stats: RunStats
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reason in ("finish", "assistant_text")


class Harness:
    def __init__(self, *, provider: Provider, config: HarnessConfig,
                 registry: ToolRegistry | None = None,
                 approval: ApprovalGate | None = None,
                 provider_factory: Callable[[], Provider] | None = None):
        self.provider = provider
        self.config = config
        self.registry = registry or default_registry(allow_bash=config.allow_bash)
        self.approval = approval or ApprovalGate()
        self._provider_factory = provider_factory

    # ================================================================ 入口
    def run(self, task: str, *, run_id: str | None = None,
            run_dir: Path | None = None) -> RunResult:
        run_id = run_id or new_run_id()
        run_dir = Path(run_dir) if run_dir else Path(self.config.runs_root) / run_id
        session = Session(run_dir)
        try:
            if not session.events:
                session.append(EventType.RUN_START, {
                    "task": task,
                    "system": self.config.system,
                    "config": self.config.to_dict(self.provider.name, 0),
                    "started": _now(),
                })
                session.append(EventType.USER_INPUT, {"text": task}, turn=0)
            return self._drive(session)
        finally:
            session.close()

    def resume(self, run_id_or_dir: str | Path, *, runs_root: Path | None = None) -> RunResult:
        p = Path(run_id_or_dir)
        run_dir = p if p.exists() else Path(runs_root or self.config.runs_root) / p
        session = Session(run_dir)
        try:
            if not session.events:
                raise FileNotFoundError(f"{run_dir} 里没有事件日志")
            return self._drive(session)
        finally:
            session.close()

    # ================================================================ 状态机
    def _next_action(self, session: Session) -> str:
        """纯函数式判定下一步。这是续跑正确性的全部来源。"""
        if session.log.of_type(EventType.RUN_STOP):
            return "done"
        if Approvals(session.events).pending():
            return "approval_pending"
        pending = self._pending_calls(session)
        if pending:
            return "exec_tools"
        resp = self._last_response(session)
        if resp is not None and not (resp.data.get("tool_calls") or []):
            return "finalize_text"
        return "call_model"

    def _last_response(self, session: Session) -> Event | None:
        for ev in reversed(session.events):
            if ev.type == EventType.MODEL_RESPONSE:
                return ev
        return None

    def _pending_calls(self, session: Session) -> list[dict[str, Any]]:
        """最后一个 assistant 响应里还没有记录 tool/result 的调用。"""
        resp = self._last_response(session)
        if resp is None:
            return []
        calls = resp.data.get("tool_calls") or []
        if not calls:
            return []
        after = [e for e in session.events if e.seq > resp.seq]
        satisfied = {e.data.get("call_id") for e in after
                     if e.type == EventType.TOOL_RESULT}
        return [c for c in calls if c.get("id") not in satisfied]

    # ================================================================ 主驱动
    def _drive(self, session: Session) -> RunResult:
        cfg = session.config()
        budget = Budget.from_config(cfg)
        workspace = Path(cfg.get("workspace") or self.config.workspace)
        depth = int(cfg.get("depth") or 0)
        ctx = ToolContext(workspace=workspace, session=session,
                          approval=self.approval, depth=depth, budget=budget)
        ctx.spawn = lambda call_ctx, task: self._spawn(session, call_ctx, task)

        if self._next_action(session) == "approval_pending":
            return self._result(session, interrupted="approval_pending")

        if session.log.last is not None and session.log.last.type != EventType.RUN_STOP \
                and len(session.events) > 2:
            session.append(EventType.NOTICE, {
                "level": "info",
                "text": f"从日志恢复执行（seq={session.log.next_seq}），继续未完成的步骤",
            })

        while True:
            action = self._next_action(session)

            if action == "done":
                return self._result(session)

            if action == "finalize_text":
                resp = self._last_response(session)
                session.append(EventType.RUN_STOP, {
                    "reason": "assistant_text",
                    "detail": (resp.data.get("text") if resp else "") or "",
                }, turn=session.log.last_turn)
                return self._result(session)

            if action == "exec_tools":
                pending = self._pending_calls(session)
                origin = self._last_response(session)
                session.append(EventType.NOTICE, {
                    "level": "warn",
                    "text": f"检测到 {len(pending)} 个中断的工具调用，重新执行（工具应幂等）",
                }, turn=session.log.last_turn)
                if self._exec_calls(session, pending, ctx, session.log.last_turn,
                                    response_seq=origin.seq, resuming=True):
                    return self._result(session, interrupted="approval_pending")
                continue

            # ---------------- call_model ----------------
            stop_reason = self._budget_stop(session, budget)
            if stop_reason:
                session.append(EventType.NOTICE, {
                    "level": "warn", "text": f"预算耗尽（{stop_reason}），停止",
                }, turn=session.log.last_turn)
                session.append(EventType.RUN_STOP, {
                    "reason": stop_reason,
                    "detail": self._best_effort_answer(session),
                }, turn=session.log.last_turn)
                return self._result(session)

            turn = session.log.last_turn + 1
            session.append(EventType.CONTEXT_SNAPSHOT, {"facts": self._facts(ctx)},
                           turn=turn)

            # 先剪枝（assemble 内），再摘要（这里）
            compacted = compact_if_needed(
                session, budget, policy=self.config.compact_policy,
                summarize=self._summarizer())
            if compacted:
                session.append(EventType.NOTICE, {
                    "level": "info", "text": "已折叠最早若干轮次为摘要（原始事件保留）",
                }, turn=turn)

            asm = assemble(session, budget)
            session.append(EventType.MODEL_REQUEST,
                           asm.header(provider=self.provider.name,
                                      model=getattr(self.provider, "model", "n/a"),
                                      tool_names=self.registry.names()), turn=turn)

            started = time.perf_counter()
            try:
                resp = self.provider.complete(asm.messages, self.registry.specs(),
                                              temperature=self.config.temperature)
            except SimulatedCrash:
                raise
            except LLMError as exc:
                # 重试用完只说明"现在调不通"（断网、限流、key 失效），不代表任务结束。
                # 不写 run/stop：日志尾部停在 model/request，resume 会从这里重新请求。
                session.append(EventType.ERROR, {"where": "model", "message": str(exc),
                                                 "resumable": True}, turn=turn)
                return self._result(session, interrupted="llm_error")

            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            origin = session.append(EventType.MODEL_RESPONSE,
                                    {**resp.as_event_data(), "latency_ms": latency_ms}, turn=turn)

            if not resp.tool_calls:
                session.append(EventType.RUN_STOP, {
                    "reason": "assistant_text", "detail": resp.text or "",
                }, turn=turn)
                return self._result(session)

            if self._exec_calls(session, origin.data["tool_calls"], ctx, turn,
                                response_seq=origin.seq):
                return self._result(session, interrupted="approval_pending")

    # ================================================================ 工具执行
    def _exec_calls(self, session: Session, calls: list[dict[str, Any]],
                    ctx: ToolContext, turn: int, *, response_seq: int,
                    resuming: bool = False) -> bool:
        """执行一条模型响应里的工具调用，等待审批时返回 True。

        调度规则：连续的 parallel_safe 调用组成一批并发执行，其余调用单独串行执行。
        写入串行的含义是：一个有副作用的调用开始前，它前面的调用都已经结束。
        无论执行顺序如何，事件一律按调用顺序写入日志，所以投影出的消息是确定的。
        """
        items: list[tuple[dict[str, Any], ToolCall | None, ToolError | None]] = []
        for raw in calls:
            try:
                items.append((raw, ToolCall.parse({
                    "id": raw.get("id"),
                    "function": {"name": raw.get("name"),
                                 "arguments": raw.get("args") or "{}"},
                }), None))
            except ToolError as exc:
                items.append((raw, None, exc))

        denials = {event.data["call_id"]: event.data["reason"] for event in session.events
                   if event.seq > response_seq and event.type == EventType.GUARD_DENY}
        turn_closed = bool(denials)
        i = 0
        while i < len(items):
            raw, call, parse_error = items[i]
            if turn_closed:
                # 必须补齐 tool 消息，否则下一次请求不符合协议
                content = denials.get(raw.get("id"), "本轮已因守卫拒绝提前结束，该调用未执行。请重新规划。")
                self._append_synthetic(session, turn, raw, skipped=True, content=content)
                i += 1
                continue

            if parse_error is not None:
                # 非法 JSON 参数和参数校验失败同级：结果回灌，模型自己修，不关轮次。
                # 不能让异常冒出去，否则 run 中断，续跑时又会撞上同一个调用而卡死。
                raw_args = str(raw.get("args"))
                session.append(EventType.TOOL_CALL, {
                    "call_id": raw.get("id"), "name": raw.get("name"),
                    "args": {}, "raw_args": raw_args[:2000], "parse_error": True,
                }, turn=turn)
                session.append(EventType.TOOL_RESULT, {
                    "call_id": raw.get("id"), "ok": False,
                    "content": (f"{parse_error}。原始参数：{raw_args[:300]}。"
                                "请改成合法的 JSON 对象后重新调用。"),
                    "artifact": None, "truncated": False,
                }, turn=turn)
                i += 1
                continue

            if resuming and not self._replay_safe(call):
                self._append_synthetic(session, turn, raw, args=call.args, content=(
                    f"进程中断后，{call.name} 依赖的外部会话（如浏览器页面）已经重置，"
                    "这个调用没有重新执行。请先恢复需要的状态（例如重新打开页面）再继续。"))
                i += 1
                continue

            batch = [call]
            j = i + 1
            if self._parallel_safe(call):
                while (j < len(items) and len(batch) < max(1, self.config.max_parallel)
                       and items[j][2] is None and self._parallel_safe(items[j][1])
                       and (not resuming or self._replay_safe(items[j][1]))):
                    batch.append(items[j][1])
                    j += 1

            for c in batch:
                data: dict[str, Any] = {"call_id": c.id, "name": c.name, "args": c.args}
                if len(batch) > 1:
                    data["parallel"] = len(batch)
                session.append(EventType.TOOL_CALL, data, turn=turn)

            results = self._run_batch(batch, ctx, response_seq)

            for c, (outcome, elapsed_ms) in zip(batch, results):
                if isinstance(outcome, ApprovalPaused):
                    if outcome.request is not None:
                        session.append(EventType.APPROVAL_REQUEST, {
                            "call_key": f"{response_seq}-{c.id}", "tool": c.name,
                            **asdict(outcome.request),
                        }, turn=turn)
                    return True
                if outcome.denied_by:
                    session.append(EventType.GUARD_DENY, {
                        "call_id": c.id, "guard": outcome.denied_by,
                        "reason": outcome.content,
                    }, turn=turn)
                session.append(EventType.TOOL_RESULT, {
                    "call_id": c.id, "ok": outcome.ok, "content": outcome.content,
                    "artifact": outcome.artifact, "truncated": outcome.truncated,
                    "elapsed_ms": elapsed_ms,
                }, turn=turn)
                if outcome.terminal:
                    session.append(EventType.RUN_STOP, {
                        "reason": "finish" if outcome.ok else "tool_error", "detail": outcome.content,
                    }, turn=turn)
                    return False
                if outcome.close_turn:
                    turn_closed = True
            i = j
        return False

    def _parallel_safe(self, call: ToolCall) -> bool:
        spec = self.registry.get(call.name)
        return bool(spec and spec.parallel_safe)

    def _replay_safe(self, call: ToolCall) -> bool:
        spec = self.registry.get(call.name)
        return spec is None or spec.replay_safe

    def _run_batch(self, batch: list[ToolCall], ctx: ToolContext,
                   response_seq: int) -> list[tuple[Outcome | ApprovalPaused, float]]:
        """执行一批调用，按调用顺序返回 (结果, 耗时毫秒)。

        每个调用拿到自己的上下文副本，call_key 是「哪条模型响应里的哪个调用」：
        续跑时不变，子 agent 用它找回自己的日志，大结果用它命名落盘文件。
        """
        approvals = Approvals(ctx.session.events) if ctx.session.config().get("async_approval") else None

        def run_one(call: ToolCall) -> tuple[Outcome | ApprovalPaused, float]:
            key = f"{response_seq}-{call.id}"
            gate = JournalApproval(approvals, key) if approvals is not None else ctx.approval
            call_ctx = replace(ctx, call_key=key, approval=gate)
            started = time.perf_counter()
            try:
                outcome = self.registry.execute(call, call_ctx,
                                                preview_chars=self.config.preview_chars)
            except ApprovalPaused as exc:
                outcome = exc
            return outcome, round((time.perf_counter() - started) * 1000, 1)

        if len(batch) == 1:
            return [run_one(batch[0])]
        with ThreadPoolExecutor(max_workers=len(batch),
                                thread_name_prefix="tool") as pool:
            futures = [pool.submit(run_one, c) for c in batch]
            return [f.result() for f in futures]

    def _append_synthetic(self, session: Session, turn: int, raw: dict[str, Any], *,
                          content: str, args: dict[str, Any] | None = None,
                          skipped: bool = False) -> None:
        """为没有执行的调用补一对 tool/call + tool/result，保持消息协议合法。"""
        call_data: dict[str, Any] = {"call_id": raw.get("id"), "name": raw.get("name"),
                                     "args": args or {}}
        if skipped:
            call_data["skipped"] = True
        else:
            call_data["not_replayed"] = True
        session.append(EventType.TOOL_CALL, call_data, turn=turn)
        session.append(EventType.TOOL_RESULT, {
            "call_id": raw.get("id"), "ok": False, "content": content,
            "artifact": None, "truncated": False,
        }, turn=turn)

    # ================================================================ 上下文
    def _facts(self, ctx: ToolContext) -> dict[str, str]:
        stats = ctx.session.stats()
        facts = {
            "now": _now(),
            "workspace": str(ctx.workspace),
            "turn": f"{stats.turns + 1}/{(ctx.budget.max_turns if ctx.budget else '?')}",
        }
        if ctx.depth:
            facts["role"] = f"subagent(depth={ctx.depth})"
        return facts

    def _summarizer(self) -> Callable[[str, str], str] | None:
        """默认不花模型调用做摘要：确定性抽取式，保证可复现。"""
        return None

    def _budget_stop(self, session: Session, budget: Budget) -> str | None:
        stats = session.stats()
        if stats.turns >= budget.max_turns:
            return "budget_turns"
        if stats.tool_calls >= budget.max_tool_calls:
            return "budget_tool_calls"
        if stats.tokens_in + stats.tokens_out >= budget.max_total_tokens:
            return "budget_tokens"
        if deadline_exceeded(session, budget):
            return "deadline"
        return None

    def _best_effort_answer(self, session: Session) -> str:
        for ev in reversed(session.events):
            if ev.type == EventType.MODEL_RESPONSE and ev.data.get("text"):
                return ev.data["text"]
            if ev.type == EventType.TOOL_RESULT and ev.data.get("ok"):
                pass
        return "(未产出最终答案)"

    # ================================================================ 子 agent
    def _spawn(self, parent: Session, ctx: ToolContext, task: str) -> Outcome:
        cfg = parent.config()
        max_depth = int(cfg.get("max_subagent_depth") or 0)
        if ctx.depth >= max_depth:
            return Outcome(ok=False, content=(
                f"已达子 agent 深度上限（depth={ctx.depth}, max={max_depth}）。"
                "请自己完成这个子任务，或把任务拆得更小后串行推进。"))

        # id 由触发它的调用决定：父 run 续跑时重新执行同一个调用，会接上原来的子日志
        # 继续跑（子 agent 已经跑完就直接取回结论），而不是开新目录从头再来。
        sub_id = sub_id_for_call(ctx.call_key) if ctx.call_key else new_sub_id()
        child_dir = child_run_dir(parent.run_dir, sub_id)
        resumed = (child_dir / "events.jsonl").exists()
        parent.append(EventType.SUBAGENT_SPAWN,
                      {"sub_id": sub_id, "task": wrap_task(task)[:500],
                       "depth": ctx.depth + 1, "resumed": resumed},
                      turn=parent.log.last_turn)
        skills = cfg.get("skills")
        child_config = HarnessConfig(
            workspace=ctx.workspace,
            runs_root=child_dir.parent,
            system=skill_system(SUBAGENT_SYSTEM, skills),
            extra={"skills": skills} if skills else {},
            budget=child_budget(Budget.from_config(cfg)),
            temperature=self.config.temperature,
            allow_bash=self.config.allow_bash,
            max_subagent_depth=max_depth,   # 子树深度不变，靠判定拦住
            preview_chars=self.config.preview_chars,
            compact_policy=self.config.compact_policy,
            max_parallel=self.config.max_parallel,
            async_approval=bool(cfg.get("async_approval")),
        )
        provider = self._provider_factory() if self._provider_factory else self.provider
        child = Harness(provider=provider, config=child_config,
                        registry=self.registry, approval=self.approval,
                        provider_factory=self._provider_factory)
        started = time.monotonic()
        try:
            res = child.run(wrap_task(task, parent_task=parent.task()),
                            run_id=sub_id, run_dir=child_dir)
            answer = res.answer or "(子 agent 未返回结论)"
            ok = res.ok
        except SimulatedCrash:
            raise
        except Exception as exc:
            answer = f"子 agent 异常: {type(exc).__name__}: {exc}"
            ok = False
            res = None

        if res is not None and res.reason == "approval_pending":
            raise ApprovalPaused()

        elapsed = time.monotonic() - started
        parent.append(EventType.SUBAGENT_RESULT, {
            "sub_id": sub_id, "ok": ok, "result": answer,
            "turns": res.stats.turns if res else 0,
            "elapsed_s": round(elapsed, 2),
        }, turn=parent.log.last_turn)

        header = (f"[子 agent {sub_id} 完成，{res.stats.turns if res else 0} 轮, "
                  f"{elapsed:.1f}s]")
        return Outcome(ok=ok, content=f"{header}\n{answer}")

    # ================================================================ 结果
    def _result(self, session: Session, *, interrupted: str | None = None) -> RunResult:
        """interrupted 非空表示 run 暂停在可续跑的位置（没有 run/stop）。"""
        stop = session.log.of_type(EventType.RUN_STOP)
        if interrupted:
            reason, answer = interrupted, self._best_effort_answer(session)
        else:
            reason = stop[-1].data.get("reason") if stop else "unknown"
            answer = stop[-1].data.get("detail") if stop else None
        return RunResult(
            run_id=session.run_dir.name,
            run_dir=session.run_dir,
            reason=reason,
            answer=answer,
            stats=session.stats(),
            messages=session.messages(),
        )
