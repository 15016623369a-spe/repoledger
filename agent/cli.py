"""命令行入口：run / resume / replay / inspect。

    python -m agent run "调研 labs/ 下的三个方案并给出选型" --workspace ./ws
    python -m agent resume 20261124-153012-a1b2
    python -m agent replay 20261124-153012-a1b2 --messages
    python -m agent inspect 20261124-153012-a1b2
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from .approval import Approvals
from .context import Budget
from .events import EventLogBusy, EventType
from .llm import MockProvider, OpenAICompatProvider, ProtocolCheckingProvider
from .loop import DEFAULT_SYSTEM, Harness, HarnessConfig, new_run_id
from .mcp import MCPError, load_mcp_servers
from .sandbox import ApprovalGate, ApprovalRequest
from .session import Session
from .settings import ProviderConfigError
from .skills import SkillsError, discover_skills, register_skills, skill_system
from .tools import default_registry
from .usage import Pricing, collect, format_report
from .wiki import WikiError, WikiPlan, WikiProvider, plan_wiki, wiki_registry
from .wiki_mcp import WikiMCPServer
from .wiki_sources import SourceError, capture_sources, read_sources

RUN_STRATEGIES = {
    "repo-impact-v2": """仓库变更影响核验 V2
- 用题目中的文件、符号或行为限定 grep 范围。同一模块的相关线索尽量合并，
  命中行足以支持结论时直接采用，只对证据缺口用 read_file 读命中附近的行。
- 源码入口不清楚且已提供 Wiki 检索工具时，可以用具体线索定位。
  只有找到更具体的关键词才再查一次，然后到源码核对行为。
- 每次查证都对应一个待回答的问题。证据齐全时用 finish 提交结论及文件、行号；
  证据仍有缺口时，明确指出未核实的部分。遵循用户要求的输出格式。""",
}


def _provider(name: str, model: str | None):
    if name == "mock":
        def policy(messages, tools):
            from .llm import LLMResponse, tool_call
            return LLMResponse(tool_calls=[tool_call("f", "finish",
                                                     answer="mock provider：未接入真实模型")])
        return MockProvider(policy)
    return OpenAICompatProvider(model=model)


def _approval(args: argparse.Namespace) -> ApprovalGate:
    if args.yes:
        return ApprovalGate.allow_all()
    if not sys.stdin.isatty():
        print("⚠️  非交互环境且未加 --yes：所有需审批操作将被拒绝", file=sys.stderr)
        return ApprovalGate()

    def ask(req: ApprovalRequest) -> bool:
        print(f"\n\033[33m[需要审批] {req.kind}\033[0m  {req.reason}")
        print(f"  {req.detail[:500]}")
        return input("  允许? [y/N] ").strip().lower() in ("y", "yes")

    return ApprovalGate(ask)


def _print_result(res, verbose: bool) -> None:
    print("\n" + "═" * 68)
    print(f"run     : {res.run_id}")
    print(f"目录    : {res.run_dir}")
    print(f"结束原因: {res.reason}   ({'成功' if res.ok else '未成功'})")
    s = res.stats
    print(f"统计    : {s.turns} 轮 · {s.tool_calls} 次工具 · "
          f"{s.tool_errors} 错误 · {s.guard_denials} 拒绝 · "
          f"{s.compactions} 次压缩 · {s.subagents} 个子 agent")
    if s.tokens_in or s.tokens_out:
        print(f"tokens  : in={s.tokens_in} out={s.tokens_out}")
    print("─" * 68)
    print(res.answer or "(无最终答案)")
    print("─" * 68)
    print(format_report(collect(res.run_dir), Pricing.from_env()))
    print("═" * 68)
    if res.reason == "approval_pending":
        command = (f"python -m agent --runs {shlex.quote(str(res.run_dir.parent))}"
                   f" approvals list {shlex.quote(res.run_id)}")
        print(f"运行已暂停，等待审批。查看待办：\n  {command}")
    if res.reason == "llm_error":
        print(f"run 已暂停。处理日志中的错误后运行：\n"
              f"  python -m agent resume {res.run_id}")


def _tools(allow_bash: bool, mcp_config: str | None, ws: Path):
    """内置工具 + 配置里的 MCP 工具。返回 (registry, manager 或 None, 写进日志的附加信息)。"""
    registry = default_registry(allow_bash=allow_bash)
    if not mcp_config:
        return registry, None, {}
    path = Path(mcp_config).resolve()
    manager = load_mcp_servers(path, registry, cwd=ws)
    for warning in manager.warnings:
        print(f"⚠️  MCP: {warning}", file=sys.stderr)
    for name, info in manager.summary.items():
        print(f"MCP {name}: {info['server'].get('name', '?')} {info['server'].get('version', '')}"
              f"，注册 {len(info['registered'])}/{info['available']} 个工具", file=sys.stderr)
    return registry, manager, {"mcp_config": str(path), "mcp": manager.summary}


def cmd_run(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    runs = Path(args.runs).resolve()
    budget = Budget(
        max_turns=args.max_turns,
        max_tool_calls=args.max_tool_calls,
        max_input_tokens=args.max_input_tokens,
        deadline_s=args.deadline,
    )
    skills = discover_skills(Path(args.skills_dir)) if args.skills_dir else None
    provider = _provider(args.provider, args.model)
    try:
        registry, manager, extra = _tools(not args.no_bash, args.mcp_config, ws)
    except MCPError as exc:
        print(f"MCP 启动失败：{exc}", file=sys.stderr)
        return 2
    register_skills(registry, skills)
    if skills:
        extra["skills"] = skills
    system = skill_system(DEFAULT_SYSTEM, skills)
    if args.strategy is not None:
        system = f"{system}\n\n{RUN_STRATEGIES[args.strategy]}"
        extra["strategy"] = args.strategy
    cfg = HarnessConfig(workspace=ws, runs_root=runs, budget=budget,
                        system=system,
                        max_subagent_depth=args.max_depth,
                        allow_bash=not args.no_bash,
                        max_parallel=args.max_parallel, extra=extra,
                        async_approval=args.async_approval)
    harness = Harness(provider=provider, config=cfg,
                      registry=registry, approval=None if cfg.async_approval else _approval(args))
    try:
        res = harness.run(args.task, run_id=args.run_id)
    except KeyboardInterrupt:
        print("\n中断。用 `python -m agent resume <run_id>` 继续。", file=sys.stderr)
        return 130
    finally:
        if manager:
            manager.close()
    _print_result(res, args.verbose)
    return 0 if res.ok else 1


def _wiki_harness(root: Path, runs: Path, plan: WikiPlan, *,
                  author: OpenAICompatProvider | None = None) -> Harness:
    if author is None and plan["provider"] == "openai":
        author = OpenAICompatProvider(model=plan["model"])
    provider = ProtocolCheckingProvider(WikiProvider(root, plan, author=author))
    cfg = HarnessConfig(workspace=root, runs_root=runs, allow_bash=False, async_approval=True,
                        max_subagent_depth=0, extra={"wiki": plan})
    return Harness(provider=provider, config=cfg, registry=wiki_registry(plan))


def cmd_wiki(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
    run_id = args.run_id or new_run_id()
    if Path(run_id).name != run_id or run_id in ("", ".", ".."):
        raise WikiError("Wiki run ID 应为单个目录名。")
    if (runs / run_id / "events.jsonl").exists():
        raise WikiError(f"run {run_id} 已存在。请使用 resume 或新 run ID。")
    author = OpenAICompatProvider(model=args.model) if args.provider == "openai" else None
    model = author.model if author is not None else None
    repository = Path(args.repository)
    options = {"provider": args.provider, "model": model}
    if args.web_sources:
        options["web_sources"] = read_sources(Path(args.web_sources))
    elif args.verify_url:
        browser_config = (Path(args.browser_config).resolve() if args.browser_config else
                          Path(__file__).resolve().parents[1] / "examples/mcp.playwright.json")
        options["web_sources"] = capture_sources(repository.resolve(), browser_config, args.verify_url)
    root, plan = plan_wiki(repository, **options)
    print(f"Wiki run {run_id} · {len(plan['pages'])} 个待更新页面", flush=True)
    harness = _wiki_harness(root, runs, plan, author=author)
    try:
        result = harness.run("生成仓库 Wiki 和 AGENTS.md 草稿", run_id=run_id)
    except KeyboardInterrupt:
        print(f"\nWiki 已中断。运行 `python -m agent --runs {shlex.quote(str(runs))}"
              f" resume {shlex.quote(run_id)}` 继续。", file=sys.stderr)
        return 130
    _print_result(result, False)
    return 0 if result.ok else 1


def cmd_wiki_mcp(args: argparse.Namespace) -> int:
    repository = Path(args.repository).resolve()
    if not repository.is_dir():
        raise WikiError(f"找不到 Wiki 所在仓库：{repository}")
    WikiMCPServer(repository).serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
    run_dir = runs / args.run_id
    if not (run_dir / "events.jsonl").is_file():
        raise FileNotFoundError(f"没有找到运行日志：{run_dir}")
    with Session(run_dir) as probe:
        cfg_dict = probe.config()
    ws = Path(cfg_dict.get("workspace") or args.workspace).resolve()
    if "wiki" in cfg_dict:
        try:
            res = _wiki_harness(ws, runs, cfg_dict["wiki"]).resume(runs / args.run_id)
        except KeyboardInterrupt:
            print(f"\nWiki 已中断。运行 `python -m agent --runs {shlex.quote(str(runs))}"
                  f" resume {shlex.quote(args.run_id)}` 继续。", file=sys.stderr)
            return 130
        _print_result(res, args.verbose)
        return 0 if res.ok else 1
    allow_bash = bool(cfg_dict.get("allow_bash", True))
    # 续跑默认重新连接原 run 用过的 MCP Server，保证工具集合一致
    provider = _provider(args.provider, args.model)
    try:
        registry, manager, extra = _tools(allow_bash, args.mcp_config or cfg_dict.get("mcp_config"), ws)
    except MCPError as exc:
        print(f"MCP 启动失败：{exc}", file=sys.stderr)
        return 2
    register_skills(registry, cfg_dict.get("skills"))
    cfg = HarnessConfig(
        workspace=ws,
        runs_root=runs,
        budget=Budget.from_config(cfg_dict),
        max_subagent_depth=int(cfg_dict.get("max_subagent_depth") or 1),
        allow_bash=allow_bash,
        max_parallel=int(cfg_dict.get("max_parallel") or 4),
        async_approval=bool(cfg_dict.get("async_approval")),
        extra=extra,
    )
    harness = Harness(provider=provider, config=cfg,
                      registry=registry, approval=None if cfg.async_approval else _approval(args))
    try:
        res = harness.resume(runs / args.run_id)
    except KeyboardInterrupt:
        print("\n中断。用 `python -m agent resume <run_id>` 继续。", file=sys.stderr)
        return 130
    finally:
        if manager:
            manager.close()
    _print_result(res, args.verbose)
    return 0 if res.ok else 1


def _approval_items(session: Session, prefix: str = ""):
    state = Approvals(session.events)
    for request in state.pending():
        yield f"{prefix}{request.seq}", request, session.run_dir
    if state.stopped:
        return
    children = {ev.data["sub_id"] for ev in session.log.of_type(EventType.SUBAGENT_SPAWN)}
    for sub_id in sorted(children):
        path = session.run_dir / "subagents" / sub_id
        if (path / "events.jsonl").is_file():
            with Session(path) as child:
                child_prefix = f"{prefix.removesuffix(':')}/" if prefix else ""
                yield from _approval_items(child, f"{child_prefix}subagents/{sub_id}:")


def _record_decision(session: Session, request_seq: int, approved: bool) -> None:
    state = Approvals(session.events)
    if not any(request.seq == request_seq for request in state.pending()):
        raise ValueError("审批请求已处理或不存在")
    session.append(EventType.APPROVAL_DECISION, {"request_seq": request_seq, "approved": approved})


def cmd_approvals(args: argparse.Namespace) -> int:
    root = Path(args.runs).resolve()
    path = root / args.run_id
    if (Path(args.run_id).name != args.run_id or path.resolve().parent != root
            or not (path / "events.jsonl").is_file()):
        print(f"没有找到运行日志：{args.run_id}", file=sys.stderr)
        return 2
    try:
        with Session(path) as session:
            items = list(_approval_items(session))
            if args.action == "list":
                for selector, request, _ in items:
                    data = {key: request.data[key] for key in ("tool", "kind", "detail", "reason")}
                    print(selector, json.dumps(data, ensure_ascii=False))
                if not items:
                    print("没有待审批项。")
                return 0
            for selector, request, request_dir in items:
                if selector != args.request:
                    continue
                if request_dir == path:
                    _record_decision(session, request.seq, args.action == "approve")
                else:
                    with Session(request_dir) as child:
                        _record_decision(child, request.seq, args.action == "approve")
                print(f"{args.action} {args.request}")
                return 0
            raise ValueError("审批请求已处理或不存在")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_replay(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
    if args.usage:
        # 只读日志文件，不打开 Session（打开会截断坏行、追加提示）
        print(format_report(collect(runs / args.run_id), Pricing.from_env()))
        return 0
    session = Session(runs / args.run_id)
    try:
        if args.messages:
            for i, m in enumerate(session.messages()):
                content = (m.get("content") or "")[:600]
                print(f"\n[{i}] role={m['role']}")
                if m.get("tool_calls"):
                    for c in m["tool_calls"]:
                        print(f"    → {c['function']['name']}({c['function']['arguments'][:200]})")
                if content:
                    print("   ", content.replace("\n", "\n    "))
        elif args.events:
            for ev in session.events:
                head = json.dumps(ev.data, ensure_ascii=False)[:180]
                print(f"{ev.seq:>4} t{ev.turn:<3} {ev.type:<18} {head}")
        else:
            st = session.stats()
            print(json.dumps({"run": args.run_id, "task": session.task(),
                              "stats": st.as_dict()}, ensure_ascii=False, indent=2))
    finally:
        session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="最小可续跑 agent harness")
    p.add_argument("--runs", default="./runs", help="runs 根目录（默认 ./runs）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一个新任务")
    r.add_argument("task")
    r.add_argument("--workspace", default="./ws")
    r.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "openai"),
                   choices=["openai", "mock"])
    r.add_argument("--model", default=None)
    r.add_argument("--run-id", default=None)
    r.add_argument("--max-turns", type=int, default=40)
    r.add_argument("--max-tool-calls", type=int, default=120)
    r.add_argument("--max-input-tokens", type=int, default=60_000)
    r.add_argument("--deadline", type=float, default=3600.0)
    r.add_argument("--max-depth", type=int, default=1)
    r.add_argument("--no-bash", action="store_true")
    r.add_argument("--max-parallel", type=int, default=4,
                   help="同一批并发执行的只读工具调用数上限（默认 4）")
    r.add_argument("--mcp-config", default=None,
                   help='MCP Server 配置文件（{"mcpServers": {...}}），见 examples/mcp.playwright.json')
    r.add_argument("--skills-dir", help="启用 <目录>/<skill>/SKILL.md 下的 Skills")
    r.add_argument("--strategy", choices=RUN_STRATEGIES,
                   help="可选的仓库变更影响核验策略")
    approvals = r.add_mutually_exclusive_group()
    approvals.add_argument("--yes", "-y", action="store_true", help="自动批准所有需审批操作")
    approvals.add_argument("--async-approval", action="store_true",
                           help="需审批时暂停，由 approvals 命令决策后续跑")
    r.add_argument("--verbose", "-v", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="从日志继续一个中断的 run")
    s.add_argument("run_id")
    s.add_argument("--workspace", default="./ws")
    s.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "openai"),
                   choices=["openai", "mock"])
    s.add_argument("--model", default=None)
    s.add_argument("--mcp-config", default=None, help="默认沿用原 run 的 MCP 配置")
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_resume)

    rp = sub.add_parser("replay", help="把日志投影成消息/事件/统计")
    rp.add_argument("run_id")
    g = rp.add_mutually_exclusive_group()
    g.add_argument("--messages", action="store_true")
    g.add_argument("--events", action="store_true")
    g.add_argument("--usage", action="store_true",
                   help="成本与耗时：token、费用（需 HARNESS_PRICE_IN/OUT）、各工具耗时，含子 agent")
    rp.set_defaults(func=cmd_replay)

    wiki = sub.add_parser("wiki", help="增量生成 Git 仓库 Wiki")
    wiki.add_argument("repository")
    wiki.add_argument("--run-id", default=None)
    wiki.add_argument("--provider", choices=("offline", "openai"), default="offline",
                      help="默认本地提取，可显式使用 OpenAI 兼容模型生成正文")
    wiki.add_argument("--model", default=None)
    sources = wiki.add_mutually_exclusive_group()
    sources.add_argument("--verify-url", action="append", help="用 Playwright 核对来源，可多次指定")
    sources.add_argument("--web-sources", help="导入已采集的外部来源 JSON")
    wiki.add_argument("--browser-config", help="指定 Playwright MCP 配置（配合 --verify-url）")
    wiki.set_defaults(func=cmd_wiki)

    wiki_mcp = sub.add_parser("wiki-mcp", help="通过 stdio 提供只读 Wiki MCP 工具")
    wiki_mcp.add_argument("repository")
    wiki_mcp.set_defaults(func=cmd_wiki_mcp)

    ap = sub.add_parser("approvals", help="列出、批准或拒绝待审批项")
    actions = ap.add_subparsers(dest="action", required=True)
    for action, help_text in (("list", "列出待审批项"), ("approve", "批准"), ("deny", "拒绝")):
        parser = actions.add_parser(action, help=help_text)
        parser.add_argument("run_id")
        if action != "list":
            parser.add_argument("request", help="列表给出的请求 ID")
        parser.set_defaults(func=cmd_approvals)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (EventLogBusy, WikiError, FileNotFoundError, ProviderConfigError, SkillsError, SourceError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
