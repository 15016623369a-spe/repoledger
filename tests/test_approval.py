"""异步审批跨进程、消息协议和 MCP 守卫回归。"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent.events import EventType
from agent.llm import LLMResponse, MockProvider, ProtocolCheckingProvider, tool_call
from agent.loop import Harness, HarnessConfig
from agent.mcp import ServerConfig, domain_guard, tool_name
from agent.sandbox import ApprovalGate
from agent.session import Session
from agent.tools import Outcome, ToolSpec, default_registry


def scripted(*first_calls, next_response=None):
    def policy(messages, tools):
        results = [m for m in messages if m["role"] == "tool"]
        if not results:
            return LLMResponse(tool_calls=list(first_calls))
        if next_response and len(results) == len(first_calls):
            return next_response
        return LLMResponse(text="完成")
    return ProtocolCheckingProvider(MockProvider(policy))


class TestAsyncApproval(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.runs = self.root / "runs"

    def harness(self, provider, *, asynchronous=False, approval=None, registry=None):
        cfg = HarnessConfig(self.ws, self.runs, async_approval=asynchronous)
        return Harness(provider=provider, config=cfg, approval=approval, registry=registry)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "agent", "--runs", str(self.runs),
                               "approvals", *args], capture_output=True, text=True, check=False)

    def events(self, *types):
        with Session(self.runs / "r") as session:
            return [event for event in session.events if event.type in types]

    def test_pause_approve_in_separate_process_and_resume(self):
        provider = scripted(tool_call("write", "write_file", path="out", content="approved"),
                            tool_call("read", "read_file", path="out"))
        paused = self.harness(provider, asynchronous=True).run("test", run_id="r")
        self.assertEqual(paused.reason, "approval_pending")
        self.assertFalse((self.ws / "out").exists())
        self.assertEqual(self.events(EventType.RUN_STOP, EventType.TOOL_RESULT), [])
        request, = self.events(EventType.APPROVAL_REQUEST)
        self.assertEqual(self.cli("list", "r").returncode, 0)
        self.assertIn(str(self.ws / "out"), self.cli("list", "r").stdout)

        unchanged = (self.runs / "r" / "events.jsonl").read_bytes()
        still_paused = self.harness(provider, approval=ApprovalGate.allow_all()).resume("r")
        self.assertEqual(still_paused.reason, "approval_pending")
        self.assertEqual((self.runs / "r" / "events.jsonl").read_bytes(), unchanged)

        decision = self.cli("approve", "r", str(request.seq))
        self.assertEqual(decision.returncode, 0, decision.stderr)
        conflict = self.cli("deny", "r", str(request.seq))
        self.assertNotEqual(conflict.returncode, 0)
        finished = self.harness(provider).resume("r")
        self.assertTrue(finished.ok)
        self.assertEqual((self.ws / "out").read_text(), "approved")
        self.assertIn("approved", next(m["content"] for m in finished.messages
                                       if m.get("tool_call_id") == "read"))
        self.assertEqual(len(self.events(EventType.APPROVAL_REQUEST)), 1)
        self.assertEqual(len(self.events(EventType.APPROVAL_DECISION)), 1)
        self.assertEqual(provider.violations, [])

    def test_denial_closes_turn_even_with_yes_on_resume(self):
        provider = scripted(tool_call("write", "write_file", path="out", content="x"),
                            tool_call("another", "write_file", path="also", content="y"))
        self.harness(provider, asynchronous=True).run("test", run_id="r")
        request, = self.events(EventType.APPROVAL_REQUEST)
        denied = self.cli("deny", "r", str(request.seq))
        self.assertEqual(denied.returncode, 0, denied.stderr)

        resumed = self.harness(provider, approval=ApprovalGate.allow_all()).resume("r")
        self.assertTrue(resumed.ok)
        self.assertFalse((self.ws / "out").exists())
        self.assertFalse((self.ws / "also").exists())
        self.assertEqual(resumed.stats.guard_denials, 1)
        self.assertEqual(resumed.stats.tool_calls, 2)
        self.assertEqual(provider.violations, [])

    def test_domain_decision_survives_new_registry_and_later_calls(self):
        name = tool_name("browser", "navigate")
        seen = []
        def registry():
            reg = default_registry()
            reg.guard(domain_guard(ServerConfig("browser", "unused",
                                               url_arguments={"navigate": "url"})))
            def navigate(ctx, args):
                seen.append(args["url"])
                return Outcome(True, "opened")
            reg.register(ToolSpec(name, "", {"type": "object", "properties": {
                "url": {"type": "string"}}}, navigate))
            return reg

        first = "https://example.test/a"
        second = "https://example.test/b"
        provider = scripted(tool_call("nav", name, url=first), next_response=LLMResponse(
            tool_calls=[tool_call("nav", name, url=second)]))
        paused = self.harness(provider, asynchronous=True, registry=registry()).run("t", run_id="r")
        self.assertEqual(paused.reason, "approval_pending")
        self.assertEqual(seen, [])
        request, = self.events(EventType.APPROVAL_REQUEST)
        self.assertEqual(request.data["kind"], "network")
        approved = self.cli("approve", "r", str(request.seq))
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertTrue(self.harness(provider, registry=registry()).resume("r").ok)
        self.assertEqual(seen, [first, second])
        self.assertEqual(len(self.events(EventType.APPROVAL_REQUEST)), 1)
        self.assertEqual(provider.violations, [])

    def test_child_approval_is_listed_and_parent_resumes_child(self):
        def policy(messages, tools):
            child = (messages[0].get("content") or "").startswith("你是一个子 agent")
            if any(m["role"] == "tool" for m in messages):
                return LLMResponse(text="完成")
            if child:
                return LLMResponse(tool_calls=[tool_call("write", "write_file",
                                                        path="child", content="approved")])
            return LLMResponse(tool_calls=[tool_call("spawn", "spawn_subagent", task="write")])

        provider = ProtocolCheckingProvider(MockProvider(policy))
        paused = self.harness(provider, asynchronous=True).run("test", run_id="r")
        self.assertEqual(paused.reason, "approval_pending")
        spawn, = self.events(EventType.SUBAGENT_SPAWN)
        sub_id = spawn.data["sub_id"]
        with Session(self.runs / "r" / "subagents" / sub_id) as session:
            request, = session.log.of_type(EventType.APPROVAL_REQUEST)
        selector = f"subagents/{sub_id}:{request.seq}"
        self.assertIn(selector, self.cli("list", "r").stdout)
        self.assertEqual(self.cli("approve", "r", selector).returncode, 0)
        self.assertTrue(self.harness(provider).resume("r").ok)
        self.assertEqual((self.ws / "child").read_text(), "approved")
        self.assertEqual(provider.violations, [])

    def test_concurrent_cli_decisions_append_once(self):
        provider = scripted(tool_call("write", "write_file", path="out", content="x"))
        self.harness(provider, asynchronous=True).run("test", run_id="r")
        request, = self.events(EventType.APPROVAL_REQUEST)
        args = [sys.executable, "-m", "agent", "--runs", str(self.runs), "approvals"]
        with subprocess.Popen([*args, "approve", "r", str(request.seq)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first, \
             subprocess.Popen([*args, "deny", "r", str(request.seq)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as second:
            first.communicate(timeout=10)
            second.communicate(timeout=10)
        self.assertEqual(sum(proc.returncode == 0 for proc in (first, second)), 1)
        self.assertEqual(len(self.events(EventType.APPROVAL_DECISION)), 1)
