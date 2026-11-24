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
import sys
from pathlib import Path

from .context import Budget
from .events import EventType
from .llm import MockProvider, OpenAICompatProvider
from .loop import Harness, HarnessConfig
from .sandbox import ApprovalGate, ApprovalRequest
from .session import Session


def _provider(name: str, model: str | None):
    if name == "mock":
        from .loop import DEFAULT_SYSTEM  # noqa: F401
        def policy(messages, tools):
            from .llm import LLMResponse, tool_call
            return LLMResponse(tool_calls=[tool_call("f", "finish",
                                                     answer="mock provider：未接入真实模型")])
        return MockProvider(policy)
    from .llm import OpenAICompatProvider
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
    print("═" * 68)


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
    cfg = HarnessConfig(workspace=ws, runs_root=runs, budget=budget,
                        max_subagent_depth=args.max_depth,
                        allow_bash=not args.no_bash)
    harness = Harness(provider=_provider(args.provider, args.model), config=cfg,
                      approval=_approval(args))
    try:
        res = harness.run(args.task, run_id=args.run_id)
    except KeyboardInterrupt:
        print("\n中断。用 `python -m agent resume <run_id>` 继续。", file=sys.stderr)
        return 130
    _print_result(res, args.verbose)
    return 0 if res.ok else 1


def cmd_resume(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
    probe = Session(runs / args.run_id)
    cfg_dict = probe.config()
    task = probe.task()
    probe.close()
    ws = Path(cfg_dict.get("workspace") or args.workspace).resolve()
    cfg = HarnessConfig(
        workspace=ws,
        runs_root=runs,
        budget=Budget.from_config(cfg_dict),
        max_subagent_depth=int(cfg_dict.get("max_subagent_depth") or 1),
        allow_bash=bool(cfg_dict.get("allow_bash", True)),
    )
    harness = Harness(provider=_provider(args.provider, args.model), config=cfg,
                      approval=_approval(args))
    res = harness.resume(args.run_id, runs_root=runs)
    _print_result(res, args.verbose)
    return 0 if res.ok else 1


def cmd_replay(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
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
    r.add_argument("--yes", "-y", action="store_true", help="自动批准所有需审批操作")
    r.add_argument("--verbose", "-v", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="从日志继续一个中断的 run")
    s.add_argument("run_id")
    s.add_argument("--workspace", default="./ws")
    s.add_argument("--provider", default=os.environ.get("HARNESS_PROVIDER", "openai"),
                   choices=["openai", "mock"])
    s.add_argument("--model", default=None)
    s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_resume)

    rp = sub.add_parser("replay", help="把日志投影成消息/事件/统计")
    rp.add_argument("run_id")
    g = rp.add_mutually_exclusive_group()
    g.add_argument("--messages", action="store_true")
    g.add_argument("--events", action="store_true")
    rp.set_defaults(func=cmd_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
