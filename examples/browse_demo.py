"""真实浏览器演示：用 Playwright MCP 打开一个文档页，再演示崩溃后的续跑。

不需要模型 API key：「模型」是一个按脚本出牌的函数，最后一步从页面快照里
摘出标题和前几个小节名作为结论。需要 Node（npx）和本机的 Chrome。

    python examples/browse_demo.py [网址]

接真实模型时用 CLI，模型会自己决定打开哪些页面、怎么摘要：

    python -m agent run "打开 https://docs.python.org/3/library/json.html，概括这个模块的主要函数" \
        --mcp-config examples/mcp.playwright.json --workspace ./ws
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import Budget
from agent.events import EventType
from agent.llm import LLMResponse, MockProvider, tool_call
from agent.loop import DEFAULT_SYSTEM, Harness, HarnessConfig
from agent.mcp import load_mcp_servers, tool_name
from agent.sandbox import ApprovalGate
from agent.session import Session
from agent.tools import default_registry
from agent.usage import collect, format_report

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "mcp.playwright.json"
OUT = HERE / "_browse"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://docs.python.org/3/library/json.html"
NAV, SNAP = tool_name("playwright", "browser_navigate"), tool_name("playwright", "browser_snapshot")


def planner(messages, tools):
    """按脚本出牌：打开页面 → 取快照 → 快照太长被截断，就到完整结果里搜标题 → 写结论。

    真实模型面对的是同一个问题：文档页的快照有上百 KB，上下文里只有开头和结尾，
    正文小节在中间，必须用 read_artifact 去取。
    """
    tool_msgs = [m["content"] for m in messages if m["role"] == "tool"]
    if not tool_msgs:
        return LLMResponse(tool_calls=[tool_call("nav", NAV, url=URL)])
    if len(tool_msgs) == 1:
        return LLMResponse(tool_calls=[tool_call("snap", SNAP)])
    if len(tool_msgs) == 2:
        artifact = re.search(r"完整结果: (\S+?)，", tool_msgs[-1])
        if artifact:
            return LLMResponse(tool_calls=[tool_call(
                "grep", "read_artifact", path=artifact.group(1), pattern=r'heading "')])
    title = re.search(r"Page Title: (.+)", tool_msgs[1])
    found = list(dict.fromkeys(re.findall(r'heading "([^"]+)"', tool_msgs[-1])))[:6]
    answer = (f"目标网页：{URL}\n页面标题：{title.group(1).strip() if title else '（未取到）'}\n"
              f"正文小节：{'、'.join(found) if found else '（未取到）'}")
    return LLMResponse(tool_calls=[tool_call("fin", "finish", answer=answer)])


def harness(registry, runs: Path) -> Harness:
    cfg = HarnessConfig(workspace=OUT / "ws", runs_root=runs, allow_bash=False,
                        budget=Budget(max_turns=8, deadline_s=300))
    # 默认审批门：一律拒绝。白名单里的文档域名不需要审批，其他域名会被挡下
    return Harness(provider=MockProvider(planner), config=cfg, registry=registry,
                   approval=ApprovalGate())


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "ws").mkdir(parents=True)
    registry = default_registry(allow_bash=False)
    print(f"启动 Playwright MCP（{CONFIG.name}）…")
    with load_mcp_servers(CONFIG, registry, cwd=OUT / "ws") as mcp:
        info = mcp.summary["playwright"]
        print(f"  {info['server'].get('name')} {info['server'].get('version')}，协议 {info['protocol']}，"
              f"注册 {len(info['registered'])}/{info['available']} 个工具，启动 {info['startup_s']}s")

        print(f"\n① 打开页面并摘要：{URL}")
        res = harness(registry, OUT / "runs").run(f"打开 {URL} 并概括页面结构", run_id="browse")
        print(f"  结束原因：{res.reason}")
        print("  " + (res.answer or "").replace("\n", "\n  "))
        s = Session(res.run_dir)
        results = {ev.data["call_id"]: ev.data for ev in s.log.of_type(EventType.TOOL_RESULT)}
        s.close()
        browser_ok = all(results.get(call_id, {}).get("ok") for call_id in ("nav", "snap"))
        for result in results.values():
            if result.get("artifact"):
                print(f"  完整快照已落盘：{result['artifact']}（上下文里只放了开头和结尾）")
            if not result["ok"]:
                print(f"  工具失败：{result['call_id']}，详情见事件日志。")

        print("\n② 崩溃续跑：模拟「打开页面、取快照」这一轮执行到一半时进程被杀")
        runs = OUT / "runs"
        s = Session(runs / "crashed")
        cfg = HarnessConfig(workspace=OUT / "ws", runs_root=runs, allow_bash=False)
        s.append(EventType.RUN_START, {"task": "续跑演示", "system": DEFAULT_SYSTEM,
                                       "config": cfg.to_dict("mock", 0)})
        s.append(EventType.USER_INPUT, {"text": "续跑演示"}, turn=0)
        s.append(EventType.MODEL_RESPONSE, LLMResponse(tool_calls=[
            tool_call("n1", NAV, url=URL), tool_call("s1", SNAP)]).as_event_data(), turn=1)
        s.close()
        resumed = Harness(provider=MockProvider(lambda m, t: LLMResponse(
            tool_calls=[tool_call("f", "finish", answer="续跑完成")])),
            config=cfg, registry=registry, approval=ApprovalGate()).resume("crashed")
        s = Session(resumed.run_dir)
        resumed_results = {ev.data["call_id"]: ev.data for ev in s.log.of_type(EventType.TOOL_RESULT)}
        s.close()
        for result in resumed_results.values():
            first = result["content"].splitlines()
            print(f"  {result['call_id']}: {'成功' if result['ok'] else '未执行或失败'} · "
                  f"{(first[1] if len(first) > 1 else first[0])[:70] if first else ''}")
        print("  打开页面可以安全重放；快照依赖中断前的浏览器状态，续跑时不重放，交给模型重新规划")

        print("\n③ 成本与耗时")
        print("  " + format_report(collect(res.run_dir)).replace("\n", "\n  "))
    print(f"\n运行目录：{OUT / 'runs'}")
    replay_ok = resumed_results.get("n1", {}).get("ok") and resumed_results.get("s1", {}).get("ok") is False
    return 0 if res.ok and browser_ok and resumed.ok and replay_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
