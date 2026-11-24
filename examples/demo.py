"""端到端演示：真实多步任务 → 中途被杀 → 续跑等价 → 长跑压缩有界。

跑法：  python examples/demo.py
不需要任何 API key —— 用 MockProvider 把"模型"换成一个确定性策略函数，
这样验证的是 harness 本身的行为，而不是模型抽风。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.compact import CompactPolicy, compact_if_needed
from agent.context import Budget, assemble, estimate_messages_tokens
from agent.events import EventType
from agent.llm import LLMResponse, MockProvider, SimulatedCrash, tool_call
from agent.loop import DEFAULT_SYSTEM, Harness, HarnessConfig
from agent.session import Session

ROOT = Path(__file__).resolve().parent / "_demo"
WS = ROOT / "ws"
RUNS = ROOT / "runs"


# ---------------------------------------------------------------- 演示数据
def prepare_workspace() -> None:
    if WS.exists():
        shutil.rmtree(WS)
    (WS / "labs").mkdir(parents=True)
    (WS / "labs" / "A-vector.md").write_text(
        "# 方案 A：纯向量检索\n召回高，但口径不可追溯；更新延迟 24h。\n成本：低\n", encoding="utf-8")
    (WS / "labs" / "B-hybrid.md").write_text(
        "# 方案 B：向量 + 关键词混合\n召回略降，但能返回原文片段，可核对。\n成本：中\n", encoding="utf-8")
    (WS / "labs" / "C-graph.md").write_text(
        "# 方案 C：知识图谱\n口径最强，但构建成本高、冷启动慢。\n成本：高\n", encoding="utf-8")
    (WS / "labs" / "notes.md").write_text(
        "# 选型约束\n- 必须可核对引用\n- 2 周内可上线\n- 不接受 24h 更新延迟\n", encoding="utf-8")


# ---------------------------------------------------------------- Mock 模型
def _called(messages) -> list[str]:
    """从可见上下文里推断"已经做过哪些工具调用"。

    注意：压缩摘要里保留了 `· 调用 xxx(` 的记录，这里一并读取——
    真实模型读摘要也是这样恢复进度的。这正是"压缩不破坏证据链"的用处。
    """
    out: list[str] = []
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                out.append(c["function"]["name"])
        else:
            out.extend(re.findall(r"· 调用 ([a-z_]+)\(", m.get("content") or ""))
    return out


def planner(messages, tools) -> LLMResponse:
    called = _called(messages)
    is_sub = bool(messages) and messages[0].get("content", "").startswith("你是一个子 agent")

    if is_sub:
        if "list_dir" not in called:
            return LLMResponse(text="先看目录", tool_calls=[tool_call("s1", "list_dir", path="labs")])
        if "read_file" not in called:
            return LLMResponse(text="读约束", tool_calls=[tool_call("s2", "read_file", path="labs/notes.md")])
        return LLMResponse(text="完成", tool_calls=[tool_call("s3", "finish", answer=(
            "约束共 3 条：可核对引用 / 2 周内上线 / 不接受 24h 延迟。"
            "其中「可核对引用」是硬门槛：只有能返回原文片段的方案才满足。"))])

    if "list_dir" not in called:
        return LLMResponse(text="探明方案", tool_calls=[tool_call("p1", "list_dir", path="labs")])
    if "spawn_subagent" not in called:
        return LLMResponse(text="拆出约束梳理", tool_calls=[tool_call(
            "p2", "spawn_subagent",
            task="读 labs/notes.md，逐条列出选型约束，并指出哪一条是硬门槛")])
    if "read_file" not in called:
        return LLMResponse(text="读 B 方案", tool_calls=[tool_call("p3", "read_file", path="labs/B-hybrid.md")])
    if "write_file" not in called:
        report = ("# 选型建议\n\n选 **方案 B（向量 + 关键词混合）**。\n\n"
                  "## 理由\n- 能返回原文片段，满足硬门槛「可核对引用」\n"
                  "- 成本中，2 周内可上线\n- 方案 A 有 24h 更新延迟，违反约束\n"
                  "- 方案 C 冷启动慢，赶不上 2 周\n")
        return LLMResponse(text="写报告", tool_calls=[tool_call(
            "p4", "write_file", path="report.md", content=report)])
    return LLMResponse(text="收尾", tool_calls=[tool_call(
        "p5", "finish", answer="建议选方案 B（向量 + 关键词混合）。理由与放弃 A/C 的原因已写入 report.md。")])


TASK = "调研 labs/ 下的三个 RAG 方案，按 notes.md 的约束给出选型建议，并写成 report.md"


def build(runs: Path, *, crash_at: int | None = None) -> Harness:
    cfg = HarnessConfig(
        workspace=WS,
        runs_root=runs,
        budget=Budget(max_turns=25, max_tool_calls=60, max_input_tokens=6000,
                      max_total_tokens=400_000, deadline_s=300),
        max_subagent_depth=1,
        allow_bash=False,
    )
    return Harness(provider=MockProvider(planner, crash_at=crash_at), config=cfg,
                   provider_factory=lambda: MockProvider(planner))


def show_trace(run_dir: Path) -> None:
    s = Session(run_dir)
    for ev in s.events:
        d = ev.data
        if ev.type == EventType.TOOL_CALL:
            print(f"  t{ev.turn:<2} 🔧 {d['name']}({json.dumps(d.get('args'), ensure_ascii=False)[:64]})")
        elif ev.type == EventType.TOOL_RESULT:
            mark = "✓" if d.get("ok") else "✗"
            print(f"  t{ev.turn:<2} {mark} → {str(d.get('content'))[:78].replace(chr(10), ' ')}")
        elif ev.type == EventType.SUBAGENT_RESULT:
            print(f"  t{ev.turn:<2} 🤖 子 agent {d['sub_id']}：{str(d['result'])[:56]}…")
        elif ev.type == EventType.COMPACT_SUMMARY:
            print(f"  t{ev.turn:<2} 🗜  压缩 seq {d['covers'][0]}..{d['covers'][1]}")
        elif ev.type == EventType.RUN_STOP:
            print(f"  t{ev.turn:<2} 🏁 结束：{d['reason']}")
        elif ev.type == EventType.NOTICE:
            print(f"  t{ev.turn:<2} ·  {str(d.get('text'))[:66]}")
    s.close()


def _tool_seq(run_dir: Path) -> list[str]:
    s = Session(run_dir)
    seq = [e.data["name"] for e in s.events if e.type == EventType.TOOL_CALL]
    s.close()
    return seq


# ---------------------------------------------------------------- ⑤ 长跑压缩
def demo_compaction(tmp: Path) -> None:
    s = Session(tmp / "runs" / "long")
    cfg = HarnessConfig(workspace=tmp, runs_root=tmp / "runs")
    s.append(EventType.RUN_START, {"task": "把 40 个数据源逐个核对并汇总",
                                   "system": DEFAULT_SYSTEM,
                                   "config": cfg.to_dict("mock", 0)})
    s.append(EventType.USER_INPUT, {"text": "把 40 个数据源逐个核对并汇总"}, turn=0)
    budget = Budget(max_input_tokens=1200)
    policy = CompactPolicy(keep_recent=4)

    print("  轮次   压缩前est   压缩？   摘要token  可见消息")
    print("  " + "─" * 54)
    for t in range(1, 41):
        s.append(EventType.MODEL_RESPONSE, {
            "text": f"第{t}轮：读第{t}个源", "usage": {},
            "tool_calls": [{"id": f"c{t}", "name": "read_file",
                            "args": json.dumps({"path": f"src/{t}.txt"})}]}, turn=t)
        s.append(EventType.TOOL_CALL, {"call_id": f"c{t}", "name": "read_file",
                                       "args": {"path": f"src/{t}.txt"}}, turn=t)
        s.append(EventType.TOOL_RESULT, {
            "call_id": f"c{t}", "ok": True, "truncated": False,
            "artifact": f"artifacts/{t:05d}.txt", "content": "数据行 " * 100}, turn=t)
        asm = assemble(s, budget)
        did = compact_if_needed(s, budget, policy=policy)
        msgs = s.messages()
        if t % 5 == 0 or did:
            summary_tokens = sum(estimate_messages_tokens([m]) for m in msgs
                                 if "[compacted history]" in (m.get("content") or ""))
            print(f"  {t:>3}   {asm.est_tokens:>8}   {'是' if did else '—':<4}   "
                  f"{summary_tokens:>8}   {len(msgs):>6}")

    msgs = s.messages()
    summaries = [e for e in s.events if e.type == EventType.COMPACT_SUMMARY]
    asm = assemble(s, budget)
    print(f"\n  累计压缩 {len(summaries)} 次，但**同时只有 1 条摘要生效**"
          f"（滚动合并，不是层层追加）")
    print(f"  最终可见消息 {len(msgs)} 条：原始 est={estimate_messages_tokens(msgs)}，"
          f"assemble 折叠后 est={asm.est_tokens} —— 已收敛，不再随轮次增长")
    print(f"  system 提示词保留: {msgs[0]['content'] == DEFAULT_SYSTEM}")
    print(f"  原目标仍在: {'把 40 个数据源逐个核对并汇总' in json.dumps(msgs, ensure_ascii=False)}")
    print(f"  最新轮次仍在: {'第40轮' in json.dumps(msgs, ensure_ascii=False)}")
    print(f"  证据指针保留: {bool(summaries[-1].data['originals'])}（originals 共 "
          f"{len(summaries[-1].data['originals'])} 条 seq）")

    # 关键验证：被折叠的内容虽然"模型看不见了"，但仍能从原始日志完整读回
    shadowed_seq = summaries[-1].data["originals"][0]
    original = [e for e in s.events if e.seq == shadowed_seq][0]
    print(f"  证据链未断: seq={shadowed_seq} 已从模型上下文隐藏，但日志里仍可读回 "
          f"→ {json.dumps(original.data, ensure_ascii=False)[:60]}…")
    s.close()


def main() -> int:
    prepare_workspace()
    if RUNS.exists():
        shutil.rmtree(RUNS)

    print("═" * 76)
    print("① 正常跑一遍（子 agent 扇出 / 产出文件 / 正常 finish 收尾）")
    print("═" * 76)
    clean = build(RUNS).run(TASK, run_id="clean")
    show_trace(clean.run_dir)
    print(f"\n  结论: {clean.answer}")
    st = clean.stats
    print(f"  统计: {st.turns} 轮 · {st.tool_calls} 次工具 · {st.subagents} 个子 agent "
          f"· 结束原因 {clean.reason}")
    print(f"  产出: {(WS / 'report.md').exists() and 'report.md ✓'}")

    print("\n" + "═" * 76)
    print("② 同一任务，第 3 次模型调用时进程被杀（模拟 OOM / Ctrl-C / 断电）")
    print("═" * 76)
    try:
        build(RUNS, crash_at=3).run(TASK, run_id="crashed")
    except SimulatedCrash as exc:
        print(f"  💥 {exc}")
    s = Session(RUNS / "crashed")
    print(f"  崩溃时日志已 fsync {len(s.events)} 条事件，最后一条: {s.log.last.type}")
    s.close()

    print("\n" + "═" * 76)
    print("③ 续跑：只读日志，不靠任何内存状态")
    print("═" * 76)
    resumed = build(RUNS).resume("crashed")
    show_trace(resumed.run_dir)
    print(f"\n  续跑结论: {resumed.answer}")

    same_answer = clean.answer == resumed.answer
    same_tools = _tool_seq(clean.run_dir) == _tool_seq(resumed.run_dir)
    print("\n" + "─" * 76)
    print(f"  等价性  最终答案一致 {'✓' if same_answer else '✗'}   工具序列一致 {'✓' if same_tools else '✗'}")
    s = Session(resumed.run_dir); before = len(s.events); s.close()
    build(RUNS).resume("crashed")
    s = Session(resumed.run_dir); after = len(s.events); s.close()
    print(f"  幂等性  已完成再续跑新增事件 {after - before}（应为 0）")

    print("\n" + "═" * 76)
    print("④ 模型可见上下文 = 日志的纯函数（可逐字节复现）")
    print("═" * 76)
    s = Session(resumed.run_dir)
    msgs = s.messages()
    print(f"  {len(s.events)} 条事件 → 投影出 {len(msgs)} 条消息\n")
    for m in msgs[:5]:
        line = (m.get("content") or "").replace("\n", " ")[:88]
        print(f"    [{m['role']:<9}] {line}")
    print(f"    … 共 {len(msgs)} 条")
    s.close()

    print("\n" + "═" * 76)
    print("⑤ 长任务压缩：40 轮连续执行，看内存是否有界")
    print("═" * 76)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        demo_compaction(Path(d))

    print("\n  🤝 工作区产出:")
    for p in sorted(WS.rglob("*")):
        if p.is_file():
            print(f"     {p.relative_to(ROOT)}  ({p.stat().st_size} B)")
    print(f"\n  运行目录: {RUNS}")
    print("  其中 events.jsonl 就是审计凭证：不可篡改、可回放、可 diff。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
