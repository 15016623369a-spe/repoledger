"""Coverage for the optional repository impact policy entry point."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import cli
from agent.events import EventType
from agent.llm import LLMError, LLMResponse, MockProvider, ProtocolCheckingProvider, tool_call
from agent.loop import DEFAULT_SYSTEM
from agent.session import Session

STRATEGY = "repo-impact-v2"


class StrategyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root / "runs"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "config.py").write_text("LIMIT = 7\n", encoding="utf-8")

    def invoke(self, policy, *args):
        provider = ProtocolCheckingProvider(MockProvider(policy))
        with (patch("agent.cli._provider", return_value=provider),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            return cli.main(["--runs", str(self.runs), *args])

    def test_opt_in_preserves_default_prompt_and_tool_permissions(self):
        captured = []

        def answer(messages, tools):
            captured.append((messages[0]["content"], tools))
            return LLMResponse(text="done")

        for run_id, options in (("default", []), ("impact", ["--strategy", STRATEGY])):
            self.assertEqual(self.invoke(
                answer, "run", "核对限制", "--workspace", str(self.workspace),
                "--run-id", run_id, "--no-bash", *options,
            ), 0)

        self.assertEqual(captured[0][0], DEFAULT_SYSTEM)
        self.assertTrue(captured[1][0].startswith(DEFAULT_SYSTEM))
        self.assertIn("read_file", captured[1][0])
        self.assertEqual(captured[0][1], captured[1][1])
        with Session(self.runs / "default") as session:
            self.assertNotIn("strategy", session.config())
        with Session(self.runs / "impact") as session:
            self.assertEqual(session.config()["strategy"], STRATEGY)

    def test_resume_uses_the_recorded_strategy_and_grep_evidence(self):
        system = []

        def before(messages, _):
            system.append(messages[0]["content"])
            if any(message["role"] == "tool" for message in messages):
                raise LLMError("fixture interruption")
            return LLMResponse(tool_calls=[tool_call("g", "grep", pattern="LIMIT", path="config.py")])

        def after(messages, _):
            self.assertEqual(messages[0]["content"], system[0])
            self.assertIn("config.py:1: LIMIT = 7", repr(messages))
            return LLMResponse(tool_calls=[tool_call("f", "finish", answer="config.py:1 限制为 7")])

        self.assertEqual(self.invoke(
            before, "run", "核对限制", "--workspace", str(self.workspace),
            "--run-id", "resume-impact", "--strategy", STRATEGY, "--no-bash",
        ), 1)
        self.assertEqual(self.invoke(after, "resume", "resume-impact"), 0)
        with Session(self.runs / "resume-impact") as session:
            self.assertEqual(session.config()["strategy"], STRATEGY)
            calls = [event.data["name"] for event in session.events if event.type == EventType.TOOL_CALL]
            self.assertEqual(calls, ["grep", "finish"])
