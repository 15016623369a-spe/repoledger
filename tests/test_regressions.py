"""回归测试。

P0：续跑后的消息协议、工具参数编码、非法 JSON 参数、审批默认拒绝。
P1：日志尾部损坏的物理截断、模型报错可续跑与 Retry-After、子 agent 续跑、
    bash 环境变量白名单、grep 不跟随符号链接出界、危险命令清单。
P2：协议校验器本身、压缩摘要的位置、edit_file 重放幂等、read_file 超大与二进制文件。

用到模型的测试都走 StrictProvider：它按 OpenAI Chat Completions 的规则检查每次
请求的消息序列。test_core.py 里的 ScriptedProvider 不检查消息，所以这几类问题
之前一直没有被测试发现。
"""
from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest import mock

from agent import cli
from agent.context import Budget
from agent.events import EventLogCorrupted, EventType
from agent.llm import (LLMError, LLMResponse, OpenAICompatProvider, ProtocolCheckingProvider,
                       ProtocolError, ScriptedProvider, SimulatedCrash, protocol_errors,
                       tool_call)
from agent.loop import DEFAULT_SYSTEM, Harness, HarnessConfig
from agent.sandbox import ApprovalGate, ApprovalRequest, check_command, shell_env
from agent.session import Session
from agent.tools import ToolCall, ToolContext, default_registry


class StrictProvider(ScriptedProvider):
    """按脚本回复，同时把每次请求的协议问题收集到 violations（不抛异常，方便断言）。"""

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
            allow_bash: bool = False, provider_factory=None) -> Harness:
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    cfg = HarnessConfig(workspace=tmp / "ws", runs_root=tmp / "runs", allow_bash=allow_bash)
    return Harness(provider=provider, config=cfg, approval=approval,
                   provider_factory=provider_factory)


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


# ====================================================================== P1
def two_event_log(run: Path) -> None:
    s = Session(run)
    s.append(EventType.RUN_START, {"task": "t", "system": "s", "config": {}})
    s.append(EventType.USER_INPUT, {"text": "t"}, turn=0)
    s.close()


class TestEventLogTailRecovery(unittest.TestCase):
    def test_tail_half_line_is_truncated_on_disk(self):
        """修复前：截断只发生在内存里，续跑后写入的事件下次加载时全部丢失。"""
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "r"
            two_event_log(run)
            half = '{"v":1,"seq":2,"ts":"x","turn":1,"type":"run/no'
            with open(run / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(half)                       # 崩溃留下的半行，没有换行
            s = Session(run)
            self.assertEqual(len(s.events), 3)       # 2 个旧事件 + 截断提示
            s.append(EventType.NOTICE, {"text": "A"})
            s.append(EventType.NOTICE, {"text": "B"})
            s.close()

            s = Session(run)
            texts = [e.data.get("text") for e in s.events]
            s.close()
            self.assertEqual(len(texts), 5, texts)
            self.assertEqual(texts[-2:], ["A", "B"])
            self.assertIn(half, (run / "events.jsonl.corrupt").read_text(encoding="utf-8"))

    def test_mid_file_corruption_is_refused(self):
        """中间坏行不是崩溃能造成的，不能自动截掉后面的有效事件。"""
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "r"
            two_event_log(run)
            path = run / "events.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n{broken\n" + lines[1] + "\n", encoding="utf-8")
            with self.assertRaises(EventLogCorrupted):
                Session(run)

    def test_complete_last_event_without_newline_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "r"
            two_event_log(run)
            path = run / "events.jsonl"
            path.write_bytes(path.read_bytes().rstrip(b"\n"))   # 换行没来得及写
            s = Session(run)
            self.assertEqual(len(s.events), 2)
            s.append(EventType.NOTICE, {"text": "C"})
            s.close()
            s = Session(run)
            self.assertEqual([e.type for e in s.events][-1], EventType.NOTICE)
            self.assertEqual(len(s.events), 3)
            s.close()


class FailingProvider:
    name = "failing"

    def complete(self, messages, tools, *, temperature=0.0):
        raise LLMError("HTTP 503: upstream unavailable")


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://example.invalid", code, "err", headers,
                                  io.BytesIO(b"rate limited"))


class TestModelErrorsAreResumable(unittest.TestCase):
    def test_llm_error_pauses_run_and_resume_finishes(self):
        """修复前：重试用完就写 run/stop，断网半分钟这个 run 就永远续不了。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            res = harness(tmp, FailingProvider()).run("t", run_id="r")
            self.assertEqual(res.reason, "llm_error")
            self.assertFalse(res.ok)
            s = Session(res.run_dir)
            self.assertFalse(s.log.of_type(EventType.RUN_STOP))
            self.assertTrue(s.log.of_type(EventType.ERROR)[-1].data["resumable"])
            s.close()

            provider = StrictProvider([finish("网络恢复后完成")])
            resumed = harness(tmp, provider).resume("r")
            self.assertTrue(resumed.ok, resumed.reason)
            self.assertEqual(resumed.answer, "网络恢复后完成")
            self.assertEqual(provider.violations, [])

    def _sleeps(self, errors):
        sleeps: list[float] = []
        provider = OpenAICompatProvider(base_url="http://example.invalid", api_key="x",
                                        max_retries=3)
        with mock.patch("agent.llm.urllib.request.urlopen", side_effect=errors), \
                mock.patch("agent.llm.time.sleep", side_effect=sleeps.append):
            with self.assertRaises(LLMError):
                provider.complete([{"role": "user", "content": "hi"}], [])
        return sleeps

    def test_retry_after_header_is_respected(self):
        sleeps = self._sleeps(lambda *a, **k: (_ for _ in ()).throw(http_error(429, "3")))
        self.assertEqual(sleeps, [3.0, 3.0], "按 Retry-After 等待，最后一次失败后不再等")

    def test_exponential_backoff_without_header(self):
        sleeps = self._sleeps(lambda *a, **k: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(sleeps, [1.0, 2.0])


class TestSubagentResume(unittest.TestCase):
    def test_parent_resume_continues_child_log(self):
        """修复前：子 agent id 随机生成，父 run 续跑时开新目录，子任务从头再跑。"""
        spawn = LLMResponse(tool_calls=[tool_call("s1", "spawn_subagent", task="列一下目录")])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            first_child = ScriptedProvider([
                LLMResponse(tool_calls=[tool_call("k1", "list_dir", path=".")]),
                finish("子结论", "kf"),
            ], crash_at=2)                       # 子 agent 第二次调模型时进程被杀
            h = harness(tmp, StrictProvider([spawn]), provider_factory=lambda: first_child)
            with self.assertRaises(SimulatedCrash):
                h.run("父任务", run_id="p")

            parent = StrictProvider([finish("父完成")])
            resumed_child = StrictProvider([finish("子结论", "kf")])
            res = harness(tmp, parent, provider_factory=lambda: resumed_child).resume("p")

            self.assertTrue(res.ok, res.reason)
            self.assertEqual(res.answer, "父完成")
            children = list((res.run_dir / "subagents").iterdir())
            self.assertEqual(len(children), 1, "续跑应接上原来的子 agent，而不是新开一个")
            child = Session(children[0])
            child_calls = [e.data["name"] for e in child.events if e.type == EventType.TOOL_CALL]
            child.close()
            self.assertEqual(child_calls, ["list_dir", "finish"], "崩溃前做完的工具不应重做")
            s = Session(res.run_dir)
            spawns = s.log.of_type(EventType.SUBAGENT_SPAWN)
            result = [e for e in s.events if e.type == EventType.TOOL_RESULT][0]
            s.close()
            self.assertEqual([e.data["resumed"] for e in spawns], [False, True])
            self.assertIn("子结论", result.data["content"])
            self.assertEqual(parent.violations + resumed_child.violations, [])


class TestShellAndSearchBoundaries(unittest.TestCase):
    SECRETS = {"HARNESS_API_KEY": "sk-h", "DEEPSEEK_API_KEY": "sk-d", "GH_TOKEN": "gh-t"}

    def test_shell_env_is_allowlisted(self):
        with mock.patch.dict(os.environ, {**self.SECRETS, "PATH": "/usr/bin", "LC_ALL": "C",
                                          "VIRTUAL_ENV": "/venv",
                                          "HARNESS_SHELL_ENV_ALLOW": "VIRTUAL_ENV"}):
            env = shell_env(Path("/tmp"))
        for key in self.SECRETS:
            self.assertNotIn(key, env)
        self.assertEqual((env["PATH"], env["LC_ALL"], env["VIRTUAL_ENV"]), ("/usr/bin", "C", "/venv"))

    def test_bash_cannot_echo_api_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, self.SECRETS):
            ctx = ToolContext(workspace=Path(d), approval=ApprovalGate.allow_all())
            out = default_registry(allow_bash=True).execute(
                ToolCall("b", "bash", {"command": 'echo "key=[$HARNESS_API_KEY]"'}), ctx)
        self.assertIn("key=[]", out.content)
        self.assertNotIn("sk-h", out.content)

    def test_grep_skips_symlinks_outside_workspace(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            ws = Path(d)
            (Path(outside) / "secret.txt").write_text("TOP_SECRET=1", encoding="utf-8")
            (ws / "leak.txt").symlink_to(Path(outside) / "secret.txt")
            (ws / "real.txt").write_text("TOP_SECRET=inside", encoding="utf-8")
            (ws / "alias.txt").symlink_to(ws / "real.txt")      # 指向 workspace 内，照常搜索
            out = default_registry(allow_bash=False).execute(
                ToolCall("g", "grep", {"pattern": "TOP_SECRET"}), ToolContext(workspace=ws))
        self.assertNotIn("TOP_SECRET=1", out.content)
        self.assertNotIn("leak.txt", out.content)
        self.assertIn("real.txt:1", out.content)
        self.assertIn("alias.txt:1", out.content)

    def test_more_dangerous_commands_denied(self):
        for cmd in ['rm -rf /*', 'rm -rf "/"', 'rm -r -f /', 'rm -rf $HOME', 'rm -rf ${HOME}/*',
                    'cd / && rm -rf .', 'cd ~; rm -r *', 'cat ~/.ssh/id_rsa',
                    'cp $HOME/.aws/credentials x']:
            self.assertTrue(check_command(cmd).denied, cmd)
        for cmd in ['rm -rf build', 'rm -rf ./dist/*', 'rm -rf /tmp/x', 'ls ~/projects',
                    'cd ~/proj && rm -rf build', 'echo /', 'ls -la /']:
            self.assertTrue(check_command(cmd).allowed, cmd)


# ====================================================================== P2
class TestProtocolChecker(unittest.TestCase):
    CALL = {"id": "a", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
    BAD = [{"role": "system", "content": "s"},
           {"role": "assistant", "content": None, "tool_calls": [CALL]},
           {"role": "user", "content": "插在中间的提示"},
           {"role": "tool", "tool_call_id": "a", "content": "r"}]

    def test_strict_wrapper_raises_on_bad_sequence(self):
        self.assertTrue(protocol_errors(self.BAD))
        with self.assertRaises(ProtocolError):
            ProtocolCheckingProvider(ScriptedProvider([finish()])).complete(self.BAD, [])

    def test_valid_sequence_passes_through(self):
        good = [self.BAD[0], self.BAD[1], self.BAD[3], self.BAD[2]]
        self.assertEqual(protocol_errors(good), [])
        resp = ProtocolCheckingProvider(ScriptedProvider([finish()])).complete(good, [])
        self.assertEqual(resp.tool_calls[0]["function"]["name"], "finish")

    def test_lenient_wrapper_only_records(self):
        p = ProtocolCheckingProvider(ScriptedProvider([finish()]), strict=False)
        p.complete(self.BAD, [])
        self.assertTrue(p.violations)


class TestSummaryPosition(unittest.TestCase):
    def test_summary_is_projected_right_after_system(self):
        """修复前：摘要按自己的 seq 投影，排在保留下来的最近几轮后面，时间线倒置。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "ws").mkdir()
            (tmp / "ws" / "big.txt").write_text("内容" * 3000, encoding="utf-8")
            script = [LLMResponse(text=f"第{i}步",
                                  tool_calls=[tool_call(f"c{i}", "read_file", path="big.txt")])
                      for i in range(8)]
            provider = StrictProvider(script + [finish()])
            cfg = HarnessConfig(workspace=tmp / "ws", runs_root=tmp / "runs",
                                budget=Budget(max_input_tokens=4000))
            res = Harness(provider=provider, config=cfg).run("反复读大文件")
        self.assertGreater(res.stats.compactions, 0)
        summaries = [i for i, m in enumerate(res.messages)
                     if (m.get("content") or "").startswith("[compacted history]")]
        self.assertEqual(summaries, [1], "唯一生效的摘要应紧跟在 system 后面")
        steps = [int(re.search(r"第(\d+)步", m["content"]).group(1))
                 for m in res.messages if m["role"] == "assistant" and m.get("content")]
        self.assertEqual(steps, sorted(steps), "摘要之后的轮次应按时间顺序排列")
        self.assertEqual(provider.violations, [])


class TestEditFileIdempotent(unittest.TestCase):
    def _edit(self, ws: Path, old: str, new: str):
        ctx = ToolContext(workspace=ws, approval=ApprovalGate.allow_all())
        return default_registry(allow_bash=False).execute(
            ToolCall("e", "edit_file", {"path": "f.txt", "old_string": old, "new_string": new}),
            ctx)

    def _twice(self, initial: str, old: str, new: str):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "f.txt").write_text(initial, encoding="utf-8")
            first, second = self._edit(ws, old, new), self._edit(ws, old, new)
            return first, second, (ws / "f.txt").read_text(encoding="utf-8")

    def test_replaying_edit_whose_new_contains_old(self):
        """修复前：foo → foo bar 重放一次会变成 foo bar bar。"""
        first, second, content = self._twice("foo\n", "foo", "foo bar")
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(content, "foo bar\n")
        self.assertIn("已是目标内容", second.content)

    def test_replaying_simple_edit_is_not_an_error(self):
        first, second, content = self._twice("a = 1\n", "a = 1", "a = 2")
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(content, "a = 2\n")

    def test_deletion_that_cannot_be_found_still_errors(self):
        _, second, content = self._twice("keep\nremove me\n", "remove me\n", "")
        self.assertFalse(second.ok, "删除型编辑无法判断是否做过，找不到原文仍应报错")
        self.assertEqual(content, "keep\n")

    def test_resume_after_edit_already_landed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            call = tool_call("e1", "edit_file", path="f.txt", old_string="foo", new_string="foo bar")
            write_interrupted_log(tmp, "r", [call], set())
            (tmp / "ws" / "f.txt").write_text("foo bar\n", encoding="utf-8")  # 已落盘，结果没写进日志
            provider = StrictProvider([finish()])
            res = harness(tmp, provider, approval=ApprovalGate.allow_all()).resume("r")
            content = (tmp / "ws" / "f.txt").read_text(encoding="utf-8")
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(content, "foo bar\n")
        self.assertEqual(provider.violations, [])


class TestReadFileLimits(unittest.TestCase):
    def test_oversized_file_result_is_still_pruned(self):
        """修复前：超过 2MB 的文件被标成 truncated，裁剪被跳过，几万字符原样进上下文。"""
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "big.log").write_text(("x" * 99 + "\n") * 25_000, encoding="utf-8")  # 2.5MB
            out = default_registry(allow_bash=False).execute(
                ToolCall("r", "read_file", {"path": "big.log"}), ToolContext(workspace=ws))
        self.assertTrue(out.ok)
        self.assertTrue(out.truncated)
        self.assertLess(len(out.content), 8000)
        self.assertIn("只读取了前 2MB", out.content)

    def test_binary_file_is_reported_as_error(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "img.bin").write_bytes(b"\x89PNG\x00\x01" * 100)
            out = default_registry(allow_bash=False).execute(
                ToolCall("r", "read_file", {"path": "img.bin"}), ToolContext(workspace=ws))
        self.assertFalse(out.ok)
        self.assertFalse(out.truncated)
        self.assertIn("二进制", out.content)


if __name__ == "__main__":
    unittest.main()
