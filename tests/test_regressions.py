"""P0 回归测试：续跑后的消息协议、工具参数编码、非法 JSON 参数、审批默认拒绝。

用到模型的测试都走 StrictProvider：它按 OpenAI Chat Completions 的规则检查每次
请求的消息序列。test_core.py 里的 ScriptedProvider 不检查消息，所以这几类问题
之前一直没有被测试发现。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from agent import cli
from agent.events import EventType
from agent.llm import LLMResponse, ScriptedProvider, tool_call
from agent.loop import DEFAULT_SYSTEM, Harness, HarnessConfig
from agent.sandbox import ApprovalGate, ApprovalRequest
from agent.session import Session


def protocol_errors(messages: list[dict]) -> list[str]:
    """带 tool_calls 的 assistant 后面必须紧跟全部对应的 tool 消息；arguments 必须是 JSON 对象。"""
    errs: list[str] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            for c in m["tool_calls"]:
                try:
                    ok = isinstance(json.loads(c["function"]["arguments"]), dict)
                except json.JSONDecodeError:
                    ok = False
                if not ok:
                    errs.append(f"[{i}] arguments 不是 JSON 对象: {c['function']['arguments'][:60]}")
            j = i + 1
            seen = []
            while j < len(messages) and messages[j]["role"] == "tool":
                seen.append(messages[j]["tool_call_id"])
                j += 1
            if sorted(seen) != sorted(ids):
                errs.append(f"[{i}] tool_calls {ids} 后紧跟的 tool 消息是 {seen}")
            i = j
            continue
        if m["role"] == "tool":
            errs.append(f"[{i}] 孤立的 tool 消息 {m['tool_call_id']}")
        i += 1
    return errs


class StrictProvider(ScriptedProvider):
    name = "strict-scripted"

    def __init__(self, script, **kw):
        super().__init__(script, **kw)
        self.violations: list[str] = []

    def complete(self, messages, tools, *, temperature=0.0):
        self.violations.extend(protocol_errors(messages))
        return super().complete(messages, tools, temperature=temperature)


def finish(answer: str = "ok", id_: str = "cf") -> LLMResponse:
    return LLMResponse(tool_calls=[tool_call(id_, "finish", answer=answer)])


def harness(tmp: Path, provider, *, approval: ApprovalGate | None = None,
            allow_bash: bool = False) -> Harness:
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    cfg = HarnessConfig(workspace=tmp / "ws", runs_root=tmp / "runs", allow_bash=allow_bash)
    return Harness(provider=provider, config=cfg, approval=approval)


def write_interrupted_log(tmp: Path, run_id: str, calls: list[dict],
                          done_ids: set[str]) -> None:
    """手写一份中断日志：模型发出了 calls，只有 done_ids 里的调用落了结果。"""
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    s = Session(tmp / "runs" / run_id)
    cfg = HarnessConfig(workspace=tmp / "ws", runs_root=tmp / "runs")
    s.append(EventType.RUN_START, {"task": "t", "system": DEFAULT_SYSTEM,
                                   "config": cfg.to_dict("strict-scripted", 0)})
    s.append(EventType.USER_INPUT, {"text": "t"}, turn=0)
    s.append(EventType.MODEL_RESPONSE, LLMResponse(tool_calls=calls).as_event_data(), turn=1)
    for c in calls:
        if c["id"] in done_ids:
            s.append(EventType.TOOL_CALL, {"call_id": c["id"], "name": c["function"]["name"],
                                           "args": json.loads(c["function"]["arguments"])}, turn=1)
            s.append(EventType.TOOL_RESULT, {"call_id": c["id"], "ok": True,
                                             "content": "done"}, turn=1)
    s.close()


class TestResumeProtocol(unittest.TestCase):
    CALLS = [tool_call("a", "list_dir", path="."), tool_call("b", "list_dir", path=".")]

    def _resume(self, done_ids: set[str]):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            write_interrupted_log(tmp, "r", self.CALLS, done_ids)
            provider = StrictProvider([finish()])
            res = harness(tmp, provider).resume("r")
            return res, provider

    def test_crash_between_two_tool_calls(self):
        """崩在两个工具调用之间：续跑提示不能插进 tool_calls 和 tool 结果中间。"""
        res, provider = self._resume({"a"})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(provider.violations, [])

    def test_crash_before_any_tool_result(self):
        res, provider = self._resume(set())
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(provider.violations, [])

    def test_resume_notices_are_kept_after_tool_results(self):
        """提示只是延后投影，不会丢。"""
        res, _ = self._resume({"a"})
        roles = [m["role"] for m in res.messages]
        first_tool = roles.index("tool")
        self.assertEqual(roles[first_tool:first_tool + 2], ["tool", "tool"])
        notices = [i for i, m in enumerate(res.messages)
                   if m["role"] == "user" and "从日志恢复执行" in (m.get("content") or "")]
        self.assertEqual(len(notices), 1)
        self.assertGreater(notices[0], first_tool + 1)


class TestToolArguments(unittest.TestCase):
    def test_arguments_are_replayed_verbatim(self):
        """回放给模型的 arguments 应该是模型原样产出的 JSON，而不是再编码一次的字符串。"""
        call = tool_call("c1", "list_dir", path=".")
        with tempfile.TemporaryDirectory() as d:
            provider = StrictProvider([LLMResponse(tool_calls=[call]), finish()])
            res = harness(Path(d), provider).run("看目录")
        replayed = [m for m in res.messages if m["role"] == "assistant"][0]
        arguments = replayed["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(arguments, call["function"]["arguments"])
        self.assertEqual(json.loads(arguments), {"path": "."})
        self.assertEqual(provider.violations, [])


class TestInvalidJsonArguments(unittest.TestCase):
    BAD = {"id": "c1", "type": "function",
           "function": {"name": "read_file", "arguments": '{"path": "a.txt"'}}

    def test_invalid_json_is_returned_to_model(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            provider = StrictProvider([LLMResponse(tool_calls=[self.BAD]), finish("改好了")])
            res = harness(tmp, provider).run("t")
            s = Session(res.run_dir)
            call = [e for e in s.events if e.type == EventType.TOOL_CALL][0]
            result = [e for e in s.events if e.type == EventType.TOOL_RESULT][0]
            s.close()
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.answer, "改好了")
        self.assertTrue(call.data.get("parse_error"))
        self.assertFalse(result.data["ok"])
        self.assertIn("不是合法 JSON", result.data["content"])
        self.assertIn('{"path": "a.txt"', result.data["content"])
        # 历史里的非法参数用 "{}" 占位，请求本身仍然合法
        self.assertEqual(provider.violations, [])

    def test_resume_is_not_stuck_on_invalid_json(self):
        """修复前：续跑会再次撞上同一个非法调用并抛异常，这个 run 永远无法继续。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            write_interrupted_log(tmp, "r", [self.BAD], set())
            provider = StrictProvider([finish("继续完成")])
            res = harness(tmp, provider).resume("r")
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.answer, "继续完成")
        self.assertEqual(provider.violations, [])


class TestApprovalFailClosed(unittest.TestCase):
    def _run(self, approval: ApprovalGate | None):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "ws").mkdir()
            (tmp / "ws" / "e.txt").write_text("old", encoding="utf-8")
            provider = StrictProvider([
                LLMResponse(tool_calls=[tool_call("w", "write_file", path="w.txt", content="hi")]),
                LLMResponse(tool_calls=[tool_call("e", "edit_file", path="e.txt",
                                                  old_string="old", new_string="new")]),
                LLMResponse(tool_calls=[tool_call("b", "bash", command="echo ran > b.txt")]),
                finish(),
            ])
            res = harness(tmp, provider, approval=approval, allow_bash=True).run("t")
            ws = tmp / "ws"
            outcome = {
                "write": (ws / "w.txt").exists(),
                "edit": (ws / "e.txt").read_text(encoding="utf-8") == "new",
                "bash": (ws / "b.txt").exists(),
                "denials": res.stats.guard_denials,
                "ok": res.ok,
                "violations": provider.violations,
            }
        return outcome

    def test_default_gate_denies_write_edit_and_bash(self):
        """没配置审批回调（Harness 默认、CLI 非交互且没加 --yes）时，三类操作都必须被拒。"""
        out = self._run(None)
        self.assertEqual((out["write"], out["edit"], out["bash"]), (False, False, False))
        self.assertEqual(out["denials"], 3)
        self.assertTrue(out["ok"], "拒绝只关当前轮次，run 仍可正常结束")
        self.assertEqual(out["violations"], [])

    def test_allow_all_permits(self):
        out = self._run(ApprovalGate.allow_all())
        self.assertEqual((out["write"], out["edit"], out["bash"]), (True, True, True))
        self.assertEqual(out["denials"], 0)

    def test_callback_decides_per_request(self):
        out = self._run(ApprovalGate(lambda req: req.kind == "write"))
        self.assertEqual((out["write"], out["edit"], out["bash"]), (True, True, False))
        self.assertEqual(out["denials"], 1)

    def test_cli_yes_approves_and_non_interactive_denies(self):
        req = ApprovalRequest("bash", "echo hi", "执行 shell 命令")
        self.assertTrue(cli._approval(Namespace(yes=True)).ask(req))
        with mock.patch.object(cli.sys, "stdin") as stdin, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            stdin.isatty.return_value = False
            gate = cli._approval(Namespace(yes=False))
        self.assertFalse(gate.ask(req))
        self.assertIn("将被拒绝", err.getvalue())


if __name__ == "__main__":
    unittest.main()
