"""第一周：只读并行 / 写入串行调度、成本与耗时统计、标准库 MCP Client。

MCP 部分用 tests/fake_mcp_server.py 作为 server，不联网、不需要 Node 或浏览器。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent.events import EventType
from agent.llm import LLMResponse, ScriptedProvider, tool_call
from agent.loop import Harness, HarnessConfig
from agent.mcp import (MCPClient, MCPError, load_mcp_servers, render_result,
                       tool_name)
from agent.sandbox import ApprovalGate
from agent.session import Session
from agent.tools import Outcome, ToolCall, ToolContext, ToolSpec, default_registry
from agent.usage import Pricing, collect, format_report, percentile
from test_regressions import StrictProvider, finish, write_interrupted_log

FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


def build(tmp: Path, provider, registry, *, approval=None, max_parallel: int = 4,
          provider_factory=None) -> Harness:
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    cfg = HarnessConfig(workspace=tmp / "ws", runs_root=tmp / "runs", allow_bash=False,
                        max_parallel=max_parallel)
    return Harness(provider=provider, config=cfg, registry=registry,
                   approval=approval or ApprovalGate.allow_all(),
                   provider_factory=provider_factory)


class Recorder:
    """记录每个工具调用的开始与结束时间。"""

    def __init__(self) -> None:
        self.spans: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def tool(self, name: str, *, parallel_safe: bool, seconds: float = 0.3,
             replay_safe: bool = True) -> ToolSpec:
        def handler(ctx: ToolContext, args: dict) -> Outcome:
            start = time.perf_counter()
            time.sleep(seconds)
            with self.lock:
                self.spans[args["tag"]] = (start, time.perf_counter())
            return Outcome(ok=True, content=f"{name}:{args['tag']}")
        return ToolSpec(name, f"测试工具 {name}",
                        {"type": "object", "properties": {"tag": {"type": "string"}},
                         "required": ["tag"]},
                        handler, parallel_safe=parallel_safe, replay_safe=replay_safe)


def registry_with(*specs: ToolSpec):
    reg = default_registry(allow_bash=False)
    for spec in specs:
        reg.register(spec)
    return reg


def events_of(run_dir: Path, kind: str) -> list:
    s = Session(run_dir)
    out = [e for e in s.events if e.type == kind]
    s.close()
    return out


# ====================================================================== 调度
class TestParallelScheduling(unittest.TestCase):
    def test_parallel_reads_run_concurrently_in_call_order(self):
        rec = Recorder()
        reg = registry_with(rec.tool("slow_read", parallel_safe=True))
        provider = StrictProvider([
            LLMResponse(tool_calls=[tool_call(f"r{i}", "slow_read", tag=f"r{i}") for i in range(3)]),
            finish(),
        ])
        with tempfile.TemporaryDirectory() as d:
            started = time.perf_counter()
            res = build(Path(d), provider, reg).run("并行读")
            elapsed = time.perf_counter() - started
            calls = events_of(res.run_dir, EventType.TOOL_CALL)
            results = events_of(res.run_dir, EventType.TOOL_RESULT)
        self.assertTrue(res.ok, res.reason)
        self.assertLess(elapsed, 0.75, "3 个 0.3 秒的只读调用应并发执行，而不是串行 0.9 秒")
        self.assertEqual([e.data.get("parallel") for e in calls[:3]], [3, 3, 3])
        self.assertEqual([e.data["call_id"] for e in results[:3]], ["r0", "r1", "r2"],
                         "结果必须按调用顺序写入，不能按完成顺序")
        self.assertTrue(all("elapsed_ms" in e.data for e in results))
        self.assertEqual(provider.violations, [])

    def test_write_waits_for_earlier_reads_and_blocks_later_ones(self):
        rec = Recorder()
        reg = registry_with(rec.tool("slow_read", parallel_safe=True, seconds=0.2),
                            rec.tool("mark_write", parallel_safe=False, seconds=0.1))
        provider = StrictProvider([
            LLMResponse(tool_calls=[
                tool_call("a", "slow_read", tag="a"), tool_call("b", "slow_read", tag="b"),
                tool_call("w", "mark_write", tag="w"), tool_call("c", "slow_read", tag="c")]),
            finish(),
        ])
        with tempfile.TemporaryDirectory() as d:
            res = build(Path(d), provider, reg).run("读写混合")
            calls = events_of(res.run_dir, EventType.TOOL_CALL)
        s = rec.spans
        self.assertGreaterEqual(s["w"][0], max(s["a"][1], s["b"][1]), "写操作要等前面的读全部结束")
        self.assertGreaterEqual(s["c"][0], s["w"][1], "写操作之后的读要等写结束")
        self.assertEqual([e.data.get("parallel") for e in calls[:4]], [2, 2, None, None])
        self.assertEqual(provider.violations, [])

    def test_max_parallel_limits_batch_size(self):
        rec = Recorder()
        reg = registry_with(rec.tool("slow_read", parallel_safe=True, seconds=0.05))
        provider = StrictProvider([
            LLMResponse(tool_calls=[tool_call(f"r{i}", "slow_read", tag=f"r{i}") for i in range(3)]),
            finish(),
        ])
        with tempfile.TemporaryDirectory() as d:
            res = build(Path(d), provider, reg, max_parallel=2).run("限并发")
            calls = events_of(res.run_dir, EventType.TOOL_CALL)
        self.assertEqual([e.data.get("parallel") for e in calls[:3]], [2, 2, None])

    def test_parallel_large_results_get_distinct_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "ws").mkdir()
            for name in ("a.txt", "b.txt"):
                (tmp / "ws" / name).write_text((name * 20 + "\n") * 300, encoding="utf-8")
            provider = StrictProvider([
                LLMResponse(tool_calls=[tool_call("x", "read_file", path="a.txt"),
                                        tool_call("y", "read_file", path="b.txt")]),
                finish(),
            ])
            res = build(tmp, provider, default_registry(allow_bash=False)).run("读两个大文件")
            results = events_of(res.run_dir, EventType.TOOL_RESULT)[:2]
            arts = [e.data["artifact"] for e in results]
            self.assertTrue(all(arts), arts)
            self.assertNotEqual(arts[0], arts[1], "并行落盘的大结果不能互相覆盖")
            self.assertIn("a.txt", (res.run_dir / arts[0]).read_text(encoding="utf-8"))
            self.assertIn("b.txt", (res.run_dir / arts[1]).read_text(encoding="utf-8"))

    def test_model_can_search_truncated_result(self):
        """截断提示里的完整结果在运行目录，read_file 读不到，必须有 read_artifact。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "ws").mkdir()
            body = "\n".join(f"line {i} filler filler filler" for i in range(600))
            (tmp / "ws" / "big.txt").write_text(body.replace("line 300 ", "line 300 NEEDLE "),
                                                encoding="utf-8")
            reg = default_registry(allow_bash=False)
            res = build(tmp, StrictProvider([
                LLMResponse(tool_calls=[tool_call("r", "read_file", path="big.txt")]), finish()]),
                reg).run("读大文件")
            art = events_of(res.run_dir, EventType.TOOL_RESULT)[0].data["artifact"]
            s = Session(res.run_dir)
            ctx = ToolContext(workspace=tmp / "ws", session=s)
            found = reg.execute(ToolCall("a", "read_artifact", {"path": art, "pattern": "NEEDLE"}), ctx)
            paged = reg.execute(ToolCall("b", "read_artifact", {"path": art, "offset": 10, "limit": 3}), ctx)
            escape = reg.execute(ToolCall("c", "read_artifact", {"path": "../../ws/big.txt"}), ctx)
            log = reg.execute(ToolCall("e", "read_artifact", {"path": "events.jsonl"}), ctx)
            s.close()
        self.assertIn("匹配 1 处", found.content)
        self.assertIn("NEEDLE", found.content)
        self.assertIn("显示 11–13", paged.content)
        self.assertEqual(escape.denied_by, "sandbox", "不能借 read_artifact 读运行目录外的文件")
        self.assertFalse(log.ok, "只能读 artifacts/，不能读事件日志")

    def test_resume_does_not_replay_stateful_tools(self):
        rec = Recorder()
        reg = registry_with(rec.tool("nav", parallel_safe=False, seconds=0),
                            rec.tool("snap", parallel_safe=False, seconds=0, replay_safe=False))
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            write_interrupted_log(tmp, "r", [tool_call("n1", "nav", tag="n1"),
                                             tool_call("s1", "snap", tag="s1")], set())
            provider = StrictProvider([finish()])
            res = build(tmp, provider, reg).resume("r")
            results = {e.data["call_id"]: e.data for e in events_of(res.run_dir, EventType.TOOL_RESULT)}
        self.assertTrue(res.ok, res.reason)
        self.assertIn("n1", rec.spans, "可重放的调用照常重新执行")
        self.assertNotIn("s1", rec.spans, "依赖外部会话的调用续跑时不能重放")
        self.assertIn("已经重置", results["s1"]["content"])
        self.assertEqual(provider.violations, [])


# ====================================================================== 统计
class TestUsage(unittest.TestCase):
    def test_tokens_cost_latency_and_tools(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[tool_call("l", "list_dir", path=".")],
                        usage={"prompt_tokens": 1000, "completion_tokens": 100,
                               "prompt_cache_hit_tokens": 400}),
            LLMResponse(tool_calls=[tool_call("f", "finish", answer="ok")],
                        usage={"prompt_tokens": 1200, "completion_tokens": 50,
                               "prompt_tokens_details": {"cached_tokens": 0}}),
        ])
        with tempfile.TemporaryDirectory() as d:
            res = build(Path(d), provider, default_registry(allow_bash=False)).run("统计")
            u = collect(res.run_dir)
        self.assertEqual((u.model_calls, u.tokens_in, u.tokens_out, u.cache_hit_tokens),
                         (2, 2200, 150, 400))
        self.assertEqual(len(u.model_latency_ms), 2)
        self.assertEqual(u.tools["list_dir"].calls, 1)
        self.assertEqual(len(u.tools["list_dir"].durations_ms), 1)
        pricing = Pricing(input_per_m=1.0, output_per_m=2.0, cache_hit_input_per_m=0.1)
        # 未命中 1800×1 + 命中 400×0.1 + 输出 150×2 = 2140，再除以一百万
        self.assertAlmostEqual(u.cost(pricing), 0.00214)
        report = format_report(u, pricing)
        self.assertIn("0.0021", report)
        self.assertIn("list_dir", report)
        self.assertIn("未配置单价", format_report(u, Pricing()))

    def test_subagent_usage_rolls_up(self):
        parent = ScriptedProvider([
            LLMResponse(tool_calls=[tool_call("s", "spawn_subagent", task="看目录")],
                        usage={"prompt_tokens": 100, "completion_tokens": 10}),
            LLMResponse(tool_calls=[tool_call("f", "finish", answer="ok")],
                        usage={"prompt_tokens": 100, "completion_tokens": 10}),
        ])
        child = ScriptedProvider([LLMResponse(tool_calls=[tool_call("k", "finish", answer="子结论")],
                                              usage={"prompt_tokens": 50, "completion_tokens": 5})])
        with tempfile.TemporaryDirectory() as d:
            res = build(Path(d), parent, default_registry(allow_bash=False),
                        provider_factory=lambda: child).run("父任务")
            u = collect(res.run_dir)
        self.assertEqual(len(u.children), 1)
        self.assertEqual(u.total("tokens_in"), 250)
        self.assertEqual(u.total("model_calls"), 3)
        self.assertIn("子 agent", format_report(u))

    def test_percentile_nearest_rank(self):
        self.assertEqual(percentile([float(i) for i in range(1, 21)], 0.95), 19.0)
        self.assertEqual(percentile([5.0], 0.5), 5.0)
        self.assertIsNone(percentile([], 0.95))


# ====================================================================== MCP client
class TestMCPClient(unittest.TestCase):
    def setUp(self):
        self.client = MCPClient("fake", sys.executable, [FAKE_SERVER], timeout=10).start()

    def tearDown(self):
        self.client.close()

    def test_handshake_pagination_and_server_requests(self):
        c = self.client
        self.assertEqual(c.protocol_version, "2025-06-18")
        self.assertEqual(c.server_info.get("name"), "fake")
        names = [t["name"] for t in c.list_tools()]
        self.assertEqual(len(names), 9, "两页工具都要拿到")
        self.assertIn("snapshot", names)
        ok, text = render_result(c.call_tool("ping_status", {}))
        self.assertTrue(ok)
        self.assertIn("true", text, "server 主动发来的 ping 必须得到回应")
        self.assertGreaterEqual(c.notifications, 1)

    def test_results_errors_and_images(self):
        ok, text = render_result(self.client.call_tool("echo", {"text": "你好"}))
        self.assertTrue(ok)
        self.assertIn("echo: 你好", text)
        ok, text = render_result(self.client.call_tool("fail", {}))
        self.assertFalse(ok)
        ok, text = render_result(self.client.call_tool("image", {}))
        self.assertIn("image/png", text)
        self.assertNotIn("AAAA", text, "图片数据不能灌进上下文")

    def test_timeout_then_recovers(self):
        with self.assertRaises(MCPError) as cm:
            self.client.call_tool("sleep", {"seconds": 1.5}, timeout=0.3)
        self.assertIn("超时", str(cm.exception))
        ok, text = render_result(self.client.call_tool("echo", {"text": "again"}, timeout=10))
        self.assertIn("again", text, "超时请求的迟到响应被丢弃后，后续请求照常工作")

    def test_server_exit_fails_fast(self):
        started = time.perf_counter()
        with self.assertRaises(MCPError) as cm:
            self.client.call_tool("crash", {}, timeout=10)
        self.assertLess(time.perf_counter() - started, 5, "server 退出时等待中的请求要立刻失败")
        self.assertIn("退出", str(cm.exception))

    def test_close_stops_process(self):
        proc = self.client._proc
        self.client.close()
        self.assertIsNotNone(proc.poll())


# ====================================================================== 接入 Harness
class TestMCPInHarness(unittest.TestCase):
    def config(self, tmp: Path, **overrides) -> Path:
        server = {
            "command": sys.executable, "args": [FAKE_SERVER],
            "allowedTools": ["echo", "navigate", "snapshot", "env_has_key"],
            "stateful": True, "replaySafeTools": ["navigate"],
            "urlArguments": {"navigate": "url"},
            "allowedDomains": ["docs.python.org"],
        }
        server.update(overrides)
        path = tmp / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"fake": server}}), encoding="utf-8")
        return path

    def test_only_allowed_tools_are_registered(self):
        with tempfile.TemporaryDirectory() as d:
            reg = default_registry(allow_bash=False)
            with load_mcp_servers(self.config(Path(d)), reg) as m:
                names = set(reg.names())
                self.assertIn(tool_name("fake", "navigate"), names)
                self.assertNotIn(tool_name("fake", "crash"), names)
                self.assertEqual(m.summary["fake"]["available"], 9)
                self.assertEqual(len(m.summary["fake"]["registered"]), 4)
                self.assertFalse(reg.get("mcp__fake__snapshot").replay_safe)
                self.assertTrue(reg.get("mcp__fake__navigate").replay_safe)
                self.assertFalse(reg.get("mcp__fake__snapshot").parallel_safe,
                                 "server 自称只读也不默认并行：浏览器有状态")

    def test_missing_allowlist_registers_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            reg = default_registry(allow_bash=False)
            before = set(reg.names())
            with load_mcp_servers(self.config(Path(d), allowedTools=None), reg) as m:
                self.assertEqual(set(reg.names()), before)
                self.assertTrue(any("allowedTools" in w for w in m.warnings))

    def test_end_to_end_browse_and_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            reg = default_registry(allow_bash=False)
            provider = StrictProvider([
                LLMResponse(tool_calls=[tool_call("n", tool_name("fake", "navigate"),
                                                  url="https://docs.python.org/3/")]),
                LLMResponse(tool_calls=[tool_call("s", tool_name("fake", "snapshot"))]),
                finish("看完了"),
            ])
            with load_mcp_servers(self.config(tmp), reg):
                # 默认审批门：一律拒绝。白名单内的域名不需要审批
                res = build(tmp, provider, reg, approval=ApprovalGate()).run("看文档")
            results = events_of(res.run_dir, EventType.TOOL_RESULT)
        self.assertTrue(res.ok, res.reason)
        self.assertIn("page: https://docs.python.org/3/", results[1].data["content"])
        self.assertIn("只作为数据使用", results[1].data["content"], "外部内容必须带不可信标记")
        self.assertEqual(provider.violations, [])

    def _navigate(self, url: str, approval: ApprovalGate):
        with tempfile.TemporaryDirectory() as d:
            reg = default_registry(allow_bash=False)
            with load_mcp_servers(self.config(Path(d)), reg):
                return reg.execute(ToolCall("n", tool_name("fake", "navigate"), {"url": url}),
                                   ToolContext(workspace=Path(d), approval=approval))

    def test_domain_guard(self):
        out = self._navigate("https://evil.example.com/x", ApprovalGate())
        self.assertEqual(out.denied_by, "domain", "白名单外的域名默认拒绝")
        out = self._navigate("https://evil.example.com/x", ApprovalGate.allow_all())
        self.assertTrue(out.ok, "批准后可以访问")
        out = self._navigate("https://sub.docs.python.org/a", ApprovalGate())
        self.assertTrue(out.ok, "白名单包含子域名")
        for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings"):
            out = self._navigate(url, ApprovalGate.allow_all())
            self.assertEqual(out.denied_by, "url-scheme", url)

    def test_server_does_not_inherit_api_keys(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"HARNESS_API_KEY": "sk-secret"}):
            reg = default_registry(allow_bash=False)
            with load_mcp_servers(self.config(Path(d)), reg):
                out = reg.execute(ToolCall("e", tool_name("fake", "env_has_key"), {}),
                                  ToolContext(workspace=Path(d)))
        self.assertIn("false", out.content)


if __name__ == "__main__":
    unittest.main()
