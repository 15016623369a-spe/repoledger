"""项目 API 配置在新进程中可读，且不会被文件工具作为任务数据读取。"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import cli
from agent.llm import OpenAICompatProvider
from agent.session import Session
from agent.sandbox import ApprovalGate
from agent.tools import ToolCall, ToolContext, default_registry


class TestProjectProviderConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dotenv = self.root / ".env"
        self.dotenv.write_text(
            'HARNESS_BASE_URL="https://endpoint.example/v1/"\n'
            "HARNESS_API_KEY=test-only-key\n"
            "HARNESS_MODEL='file-model' # comment\n"
            "IGNORED_SETTING=ignored\n", encoding="utf-8")

    def test_file_fallback_and_env_override(self):
        with mock.patch("agent.settings.DOTENV_PATH", self.dotenv), \
             mock.patch.dict(os.environ, {}, clear=True):
            provider = OpenAICompatProvider()
            self.assertEqual((provider.base_url, provider.api_key, provider.model),
                             ("https://endpoint.example/v1", "test-only-key", "file-model"))
            self.assertNotIn("HARNESS_API_KEY", os.environ)
            with mock.patch.dict(os.environ, {"HARNESS_MODEL": "env-model",
                                              "HARNESS_API_KEY": "env-key"}):
                provider = OpenAICompatProvider(model="explicit-model")
                self.assertEqual((provider.model, provider.api_key), ("explicit-model", "env-key"))
                self.assertEqual(OpenAICompatProvider().model, "env-model")

    def test_file_key_does_not_follow_base_url_override(self):
        with mock.patch("agent.settings.DOTENV_PATH", self.dotenv), \
             mock.patch.dict(os.environ, {"HARNESS_BASE_URL": "https://other.example/v1"},
                             clear=True):
            self.assertEqual(OpenAICompatProvider().api_key, "")
            self.assertEqual(OpenAICompatProvider(api_key="alternate-test-key").api_key,
                             "alternate-test-key")

    def test_wiki_persists_resolved_model(self):
        runs = self.root / "runs"
        with mock.patch("agent.settings.DOTENV_PATH", self.dotenv), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("agent.cli.plan_wiki") as plan_wiki, \
             contextlib.redirect_stdout(io.StringIO()):
            plan_wiki.return_value = self.root, {"provider": "openai", "model": "file-model",
                                                 "head": None, "pages": []}
            ret = cli.main(["--runs", str(runs), "wiki", str(self.root),
                            "--provider", "openai", "--run-id", "dotenv-check"])
        self.assertEqual(ret, 0)
        plan_wiki.assert_called_once_with(self.root, provider="openai", model="file-model")
        with Session(runs / "dotenv-check") as session:
            self.assertEqual(session.config()["wiki"]["model"], "file-model")
            self.assertNotIn("test-only-key", repr(session.events))

    def test_project_dotenv_is_hidden_from_file_tools_even_via_aliases(self):
        (self.root / "config-alias").symlink_to(self.dotenv)
        os.link(self.dotenv, self.root / "config-hardlink")
        (self.root / "readable.txt").write_text("HARNESS_API_KEY=placeholder\n", encoding="utf-8")
        registry = default_registry(allow_bash=False)
        ctx = ToolContext(workspace=self.root, approval=ApprovalGate.allow_all())
        with mock.patch("agent.settings.DOTENV_PATH", self.dotenv):
            for path in (".env", "config-alias", "config-hardlink"):
                with self.subTest(path=path):
                    for tool, args in (("read_file", {}), ("write_file", {"content": "replace"}),
                                       ("edit_file", {"old_string": "test-only-key", "new_string": "replace"})):
                        out = registry.execute(ToolCall("t", tool, {"path": path, **args}), ctx)
                        self.assertFalse(out.ok)
                    out = registry.execute(ToolCall("t", "grep", {"path": path, "pattern": "."}), ctx)
                    self.assertFalse(out.ok)
            res = registry.execute(ToolCall("g", "grep", {"pattern": "HARNESS_API_KEY"}), ctx)
        self.assertTrue(res.ok)
        self.assertIn("readable.txt", res.content)
        self.assertNotIn("test-only-key", res.content)
        self.assertIn("test-only-key", self.dotenv.read_text(encoding="utf-8"))
