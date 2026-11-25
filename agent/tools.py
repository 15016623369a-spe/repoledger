"""工具流水线：validate → guards(单调) → dispatch → prune → finalize。

失败分层（§3.4）：
  · 参数校验失败 → 普通错误结果，回灌给模型自己修，**不关轮次**；
  · 守卫拒绝     → 单调生效（第一个 deny 就是终局），并**关掉当前轮次**；
  · 执行异常     → 普通错误结果，回灌，模型可重试或换路。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .context import estimate_tokens
from .sandbox import (MAX_READ_BYTES, ApprovalRequest, CommandVerdict, SandboxViolation,
                      check_command, is_binary, resolve_within, safe_read_text,
                      shell_env)

# 单个工具结果回灌前的默认上限（字符）。超过就走 artifact + 指针。
DEFAULT_PREVIEW_CHARS = 6000
HEAD_CHARS = 4000
TAIL_CHARS = 1500


# ---------------------------------------------------------------- 数据结构
class ToolError(Exception):
    """可回灌给模型的普通错误。"""


class FatalToolError(Exception):
    """需要关轮次的策略层失败。"""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "ToolCall":
        fn = raw.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError(f"参数不是合法 JSON: {exc}") from None
        return cls(id=str(raw.get("id") or ""), name=str(fn.get("name") or ""),
                   args=args or {})


@dataclass
class Outcome:
    ok: bool
    content: str
    artifact: str | None = None
    truncated: bool = False
    terminal: bool = False
    denied_by: str | None = None      # 非 None 表示被守卫拒绝
    close_turn: bool = False


GuardFn = Callable[[ToolCall, "ToolContext"], "GuardDecision | None"]


@dataclass(frozen=True)
class GuardDecision:
    deny: bool
    guard: str
    reason: str = ""


@dataclass
class ToolContext:
    workspace: Path
    session: Any = None
    approval: Any = None
    depth: int = 0
    spawn: Callable[[str, dict[str, Any]], "Outcome"] | None = None
    budget: Any = None
    call_key: str | None = None   # 当前调用的稳定标识「响应 seq-调用 id」，由主循环设置

    def root(self) -> Path:
        return Path(self.workspace).resolve()


# ---------------------------------------------------------------- 工具定义
Handler = Callable[["ToolContext", dict[str, Any]], Outcome]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._guards: list[GuardFn] = []

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def guard(self, fn: GuardFn) -> None:
        self._guards.append(fn)

    def specs(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    # ---------- 流水线 ----------
    def execute(self, call: ToolCall, ctx: ToolContext, *,
                preview_chars: int = DEFAULT_PREVIEW_CHARS) -> Outcome:
        spec = self._tools.get(call.name)
        if spec is None:
            return Outcome(ok=False, content=(
                f"未知工具 {call.name!r}。可用工具: {', '.join(self.names())}"))

        # 1. 参数校验：失败是普通错误，不关轮次
        try:
            _validate_args(spec, call.args)
        except ToolError as exc:
            return Outcome(ok=False, content=f"参数校验失败: {exc}")

        # 2. 守卫：单调——第一个 deny 就是终局，后面的守卫无权翻案
        for guard in self._guards:
            try:
                decision = guard(call, ctx)
            except FatalToolError as exc:
                return Outcome(ok=False, content=f"策略层拒绝: {exc}",
                               close_turn=True, denied_by="policy")
            if decision and decision.deny:
                return Outcome(ok=False,
                               content=f"被守卫 {decision.guard} 拒绝: {decision.reason}",
                               denied_by=decision.guard, close_turn=True)

        # 3. 分派执行
        try:
            outcome = spec.handler(ctx, call.args)
        except SandboxViolation as exc:
            return Outcome(ok=False, content=f"沙箱越界: {exc}",
                           denied_by="sandbox", close_turn=True)
        except FatalToolError as exc:
            return Outcome(ok=False, content=f"策略层拒绝: {exc}",
                           close_turn=True, denied_by="policy")
        except ToolError as exc:
            return Outcome(ok=False, content=f"工具报错: {exc}")
        except subprocess.TimeoutExpired:
            return Outcome(ok=False, content="命令超时，已终止")
        except Exception as exc:  # 兜底：任何异常都回灌，不炸掉整个 run
            return Outcome(ok=False, content=f"工具异常 {type(exc).__name__}: {exc}")

        # 4. 裁剪（保留指针，不让"截断"看起来像"数据不完整"）
        return _prune_maybe(outcome, ctx, preview_chars)


def _validate_args(spec: ToolSpec, args: dict[str, Any]) -> None:
    if not isinstance(args, dict):
        raise ToolError("参数必须是对象")
    required = spec.parameters.get("required") or []
    for key in required:
        if key not in args:
            raise ToolError(f"缺少必填参数 {key!r}")
    props = spec.parameters.get("properties") or {}
    for key, value in args.items():
        if key not in props:
            raise ToolError(f"未知参数 {key!r}（允许: {', '.join(props)}）")
        expected = props[key].get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolError(f"{key} 应为 string，实际 {type(value).__name__}")
        if expected == "integer" and not isinstance(value, int):
            raise ToolError(f"{key} 应为 integer，实际 {type(value).__name__}")
        if expected == "object" and not isinstance(value, dict):
            raise ToolError(f"{key} 应为 object，实际 {type(value).__name__}")


def _prune_maybe(outcome: Outcome, ctx: ToolContext,
                 preview_chars: int) -> Outcome:
    if outcome.truncated or len(outcome.content) <= preview_chars:
        return outcome
    head = outcome.content[:HEAD_CHARS]
    tail = outcome.content[-TAIL_CHARS:]
    dropped = len(outcome.content) - HEAD_CHARS - TAIL_CHARS
    artifact = None
    if ctx.session is not None:
        art = ctx.session.write_artifact(ctx.session.log.next_seq, outcome.content)
        artifact = art
        note = f"\n\n[已截断 {dropped} 字符；完整结果: {art}]"
    else:
        note = f"\n\n[已截断 {dropped} 字符]"
    return Outcome(ok=outcome.ok, content=f"{head}\n\n…（中间 {dropped} 字符省略）…\n\n{tail}{note}",
                   artifact=artifact, truncated=True, terminal=outcome.terminal)


# ---------------------------------------------------------------- 内置工具
def _ok(content: str, **kw: Any) -> Outcome:
    return Outcome(ok=True, content=content, **kw)


def tool_read_file(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    path = resolve_within(ctx.root(), args["path"])
    if not path.exists():
        raise ToolError(f"文件不存在: {args['path']}")
    if path.is_dir():
        raise ToolError(f"{args['path']} 是目录，用 list_dir")
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if is_binary(head):
        raise ToolError(f"{args['path']} 是二进制文件（{path.stat().st_size} 字节），不能按文本读取")
    text, oversized = safe_read_text(path)
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 400)
    lines = text.splitlines()
    chunk = lines[offset:offset + limit]
    body = "\n".join(f"{offset + i + 1:>5}\t{ln}" for i, ln in enumerate(chunk))
    header = f"{path.name} 共 {len(lines)} 行，显示 {offset + 1}–{offset + len(chunk)}\n"
    if oversized:
        header = (f"{path.name} 超过 {MAX_READ_BYTES // 1_000_000}MB，只读取了前 "
                  f"{MAX_READ_BYTES // 1_000_000}MB（{len(lines)} 行），"
                  f"显示 {offset + 1}–{offset + len(chunk)}\n")
    # 不设 truncated：这里的"超大"指文件，不是结果已经裁剪过。结果长了照常走
    # _prune_maybe 落 artifact，否则几万字符会原样灌进上下文。
    return _ok(header + body)


def tool_list_dir(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    path = resolve_within(ctx.root(), args.get("path") or ".")
    if not path.is_dir():
        raise ToolError(f"不是目录: {path}")
    rows = []
    for child in sorted(path.iterdir()):
        if child.name in (".git", "__pycache__", "node_modules"):
            continue
        if child.is_dir():
            rows.append(f"[dir]  {child.name}/")
        else:
            rows.append(f"[file] {child.name}  ({child.stat().st_size} B)")
    return _ok("\n".join(rows) or "(空目录)")


def _approved(ctx: ToolContext, req: ApprovalRequest) -> bool:
    """需审批的操作一律经过审批门。没有审批门或未获批都算拒绝（fail closed）。"""
    return ctx.approval is not None and ctx.approval.ask(req)


def tool_write_file(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    path = resolve_within(ctx.root(), args["path"])
    content = args.get("content") or ""
    if not _approved(ctx, ApprovalRequest("write", str(path), "写文件")):
        raise FatalToolError(f"写入未获批: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return _ok(f"{'已覆盖' if existed else '已创建'} {path.relative_to(ctx.root())}"
               f"（{len(content)} 字符, {len(content.splitlines())} 行）")


def tool_edit_file(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    path = resolve_within(ctx.root(), args["path"])
    if not path.exists():
        raise ToolError(f"文件不存在: {args['path']}")
    old = args["old_string"]
    new = args.get("new_string") or ""
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    # 幂等：续跑时可能重新执行一次已经生效的编辑。判断依据是「new 已在文件里，且
    # 除了 new 内部之外不再有单独的 old」。只看 count 不够：new 包含 old 时
    # （如 foo → foo bar），重放会再替换一次变成 foo bar bar。
    # 删除型编辑（new 为空）无法从文件内容判断是否做过，不走这条。
    if new and new != old and new in text and text.replace(new, "\0").count(old) == 0:
        return _ok(f"{path.relative_to(ctx.root())} 已是目标内容（new_string 已存在，"
                   "old_string 不再单独出现），未修改")
    if count == 0:
        raise ToolError("未找到要替换的原文（注意缩进与换行必须完全一致）")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"原文出现 {count} 次，请提供更长的唯一上下文，或设置 replace_all=true")
    if not _approved(ctx, ApprovalRequest("write", str(path), "编辑文件")):
        raise FatalToolError(f"编辑未获批: {path}")
    path.write_text(text.replace(old, new) if args.get("replace_all")
                    else text.replace(old, new, 1), encoding="utf-8")
    return _ok(f"已编辑 {path.relative_to(ctx.root())}（替换 {count if args.get('replace_all') else 1} 处）")


def tool_grep(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    pattern = args["pattern"]
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"正则非法: {exc}") from None
    root = ctx.root()
    base = resolve_within(root, args.get("path") or ".")
    include = args.get("include")
    hits: list[str] = []
    files = [base] if base.is_file() else [
        p for p in base.rglob("*")
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
    ]
    for p in files:
        if include and not p.match(include):
            continue
        # 和 read_file 同一条边界：符号链接指向 workspace 外的文件一律跳过
        if not p.resolve().is_relative_to(root):
            continue
        try:
            text, _ = safe_read_text(p, max_bytes=400_000)
        except OSError:
            continue
        if text.startswith("(二进制"):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                try:
                    rel = p.relative_to(ctx.root())
                except ValueError:
                    rel = p
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    return _ok("\n".join(hits) + "\n…（命中超过 200 条，已截断）")
    return _ok("\n".join(hits) if hits else "(无匹配)")


def tool_bash(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    command = args["command"]
    timeout = int(args.get("timeout_s") or 60)
    verdict: CommandVerdict = check_command(command)
    if verdict.denied:
        raise FatalToolError(f"命令被策略拒绝：{verdict.reason}")
    if not _approved(ctx, ApprovalRequest("bash", command, "执行 shell 命令")):
        raise FatalToolError("命令未获批")
    started = time.monotonic()
    proc = subprocess.run(
        command, shell=True, cwd=str(ctx.root()), capture_output=True,
        text=True, timeout=timeout, env=shell_env(ctx.root()),
    )
    elapsed = time.monotonic() - started
    parts = [f"$ {command}", f"exit={proc.returncode}  ({elapsed:.2f}s)"]
    if proc.stdout:
        parts.append(f"--- stdout ---\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"--- stderr ---\n{proc.stderr.rstrip()}")
    body = "\n".join(parts)
    return _ok(body, truncated=False) if proc.returncode == 0 else Outcome(
        ok=False, content=body)


def tool_spawn_subagent(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    if ctx.spawn is None:
        raise ToolError("子 agent 不可用（当前上下文未提供 spawn 能力）")
    task = args["task"]
    return ctx.spawn(task, args)


def tool_finish(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    return _ok(args["answer"], terminal=True)


# ---------------------------------------------------------------- 默认工具集
def default_registry(*, allow_bash: bool = True) -> ToolRegistry:
    reg = ToolRegistry()

    reg.register(ToolSpec(
        "read_file", "读取 workspace 内文件，按行号返回。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对 workspace 的路径"},
            "offset": {"type": "integer", "description": "起始行(0-based)"},
            "limit": {"type": "integer", "description": "最多读多少行"},
        }, "required": ["path"]}, tool_read_file))

    reg.register(ToolSpec(
        "list_dir", "列出目录内容。",
        {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": []}, tool_list_dir))

    reg.register(ToolSpec(
        "write_file", "写入/覆盖文件（父目录自动创建）。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]}, tool_write_file))

    reg.register(ToolSpec(
        "edit_file", "精确替换文件中的一段原文。old_string 必须唯一匹配。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_string", "new_string"]}, tool_edit_file))

    reg.register(ToolSpec(
        "grep", "按正则搜索文件内容，返回 路径:行号: 内容。",
        {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "include": {"type": "string", "description": "glob 过滤，如 **/*.py"},
        }, "required": ["pattern"]}, tool_grep))

    if allow_bash:
        reg.register(ToolSpec(
            "bash", "在 workspace 内执行 shell 命令。返回退出码与输出。",
            {"type": "object", "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer"},
            }, "required": ["command"]}, tool_bash))

    reg.register(ToolSpec(
        "spawn_subagent", "派一个子 agent 去完成独立子任务。子 agent 有独立上下文，只返回结构化结论。",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "自包含的任务描述，子 agent 看不到你的上下文"},
        }, "required": ["task"]}, tool_spawn_subagent))

    reg.register(ToolSpec(
        "finish", "任务完成时调用，提交最终答案。这是唯一正常终止方式。",
        {"type": "object", "properties": {
            "answer": {"type": "string"},
        }, "required": ["answer"]}, tool_finish))

    return reg
