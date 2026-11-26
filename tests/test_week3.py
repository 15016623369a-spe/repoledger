"""Week 3 integration regressions."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import cli
from agent.events import EventType
from agent.llm import LLMError, LLMResponse, MockProvider, ProtocolCheckingProvider, tool_call
from agent.mcp import MCPClient, MCPError
from agent.session import Session
from agent.settings import DOTENV_PATH
from agent.tools import ToolCall, ToolContext, default_registry
from agent.skills import discover_skills, register_skills
from agent.wiki_mcp import WikiMCPServer
from agent.wiki_sources import SourceError, _host, capture_sources

ROOT = Path(__file__).resolve().parents[1]


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def skill(self, name, description="Catalog entry", body="Unique instructions"):
        folder = self.root / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: >\n  {description}\n---\n{body}\n", encoding="utf-8")


class TestWikiMCP(LocalFixture):
    def test_real_stdio_tools_and_path_boundary(self):
        wiki = self.root / "wiki"
        wiki.mkdir()
        (wiki / "architecture.md").write_text("# 项目架构\n\nMCP server\nMCP tools\n", encoding="utf-8")
        (wiki / "modules.md").symlink_to(DOTENV_PATH)
        (wiki / "decisions.md").symlink_to(wiki / "architecture.md")
        args = ["-m", "agent", "wiki-mcp", str(self.root)]
        with MCPClient("repo-wiki", sys.executable, args, cwd=ROOT, timeout=4, startup_timeout=4) as client:
            self.assertEqual({tool["name"] for tool in client.list_tools()},
                             {"list_pages", "search_wiki", "get_page"})
            def result(tool, args):
                return json.loads(client.call_tool(tool, args)["content"][0]["text"])
            self.assertEqual([p["page"] for p in result("list_pages", {})["pages"]], ["architecture.md"])
            hit, = result("search_wiki", {"query": "mcp", "limit": 1})["matches"]
            self.assertEqual((hit["page"], hit["line"]), ("architecture.md", 3))
            first = result("get_page", {"page": "architecture.md", "limit": 2})
            self.assertEqual(first["next_offset"], 2)
            second = result("get_page", {"page": "architecture.md", "offset": 2})
            self.assertEqual(first["total_chars"], len(first["markdown"] + second["markdown"]))
            self.assertIn("MCP tools", second["markdown"])
            self.assertIsNone(second["next_offset"])
            for bad in ("../.env", "wiki/architecture.md"):
                with self.assertRaises(MCPError):
                    client.call_tool("get_page", {"page": bad})
            self.assertTrue(client.call_tool("get_page", {"page": "decisions.md"})["isError"])

    def test_stdio_errors_and_notification_have_separate_lines(self):
        messages = [
            b"bad json",
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
            b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}',
        ]
        stdout = io.BytesIO()
        WikiMCPServer(self.root).serve(io.BytesIO(b"\n".join(messages) + b"\n"), stdout)
        invalid, early, initialized, tools = map(json.loads, stdout.getvalue().splitlines())
        self.assertEqual(invalid["error"]["code"], -32700)
        self.assertEqual(early["error"]["code"], -32002)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(tools["result"]["tools"]), 3)


class TestSkills(LocalFixture):
    def test_progressive_disclosure_and_resume_catalog(self):
        self.skill("wiki-help", "Find evidence", "WIKI_SKILL_BODY")
        runs = self.root / "runs"
        requests = []

        def before(messages, tools):
            requests.append(messages)
            self.assertIn("wiki-help: Find evidence", messages[0]["content"])
            self.assertNotIn("WIKI_SKILL_BODY", repr(messages))
            self.assertIn("read_skill", [tool["function"]["name"] for tool in tools])
            raise LLMError("fixture interruption")

        def after(messages, tools):
            requests.append(messages)
            self.assertNotIn("new-skill", messages[0]["content"])
            if any(message["role"] == "tool" for message in messages):
                self.assertIn("WIKI_SKILL_BODY", repr(messages))
                self.assertNotIn("WIKI_SKILL_BODY", messages[0]["content"])
                return LLMResponse(text="done")
            call = tool_call("read", "read_skill")
            call["function"]["arguments"] = json.dumps({"name": "wiki-help"})
            return LLMResponse(tool_calls=[call])

        def run(*args, policy):
            provider = ProtocolCheckingProvider(MockProvider(policy))
            with mock.patch("agent.cli._provider", return_value=provider), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                return cli.main(["--runs", str(runs), *args])

        self.assertEqual(run("run", "fixture", "--workspace", str(self.root),
                             "--skills-dir", str(self.root / "skills"),
                             "--no-bash", "--run-id", "skills", policy=before), 1)
        self.skill("new-skill")
        self.assertEqual(run("resume", "skills", policy=after), 0)
        self.assertEqual(len(requests), 3)
        with Session(runs / "skills") as session:
            self.assertEqual(list(session.config()["skills"]["entries"]), ["wiki-help"])
            self.assertTrue(any(e.type == EventType.TOOL_RESULT and "WIKI_SKILL_BODY" in e.data["content"]
                                for e in session.events))

    def test_tool_denies_unlisted_names_and_metadata_drift(self):
        self.skill("wiki-help")
        catalog = discover_skills(self.root / "skills")
        registry = default_registry(allow_bash=False)
        register_skills(registry, catalog)
        ctx = ToolContext(self.root)
        out = registry.execute(ToolCall("bad", "read_skill", {"name": "../../.env"}), ctx)
        self.assertFalse(out.ok)
        self.skill("wiki-help", "Changed metadata")
        out = registry.execute(ToolCall("changed", "read_skill", {"name": "wiki-help"}), ctx)
        self.assertFalse(out.ok)
        self.assertIn("元数据已变化", out.content)


class TestBrowserSources(LocalFixture):
    def config(self, domain="example.test"):
        path = self.root / "browser.json"
        path.write_text(json.dumps({"mcpServers": {"playwright": {
            "command": sys.executable,
            "args": [str(ROOT / "tests/fake_mcp_server.py"), "--playwright"],
            "allowedTools": ["browser_navigate", "browser_snapshot"],
            "urlArguments": {"browser_navigate": "url"},
            "allowedDomains": [domain],
            "timeout": 3,
        }}}), encoding="utf-8")
        return path

    def test_capture_through_stdio_and_check_browser_readback(self):
        url = "https://example.test/docs"
        source, = capture_sources(self.root, self.config(), [url])
        self.assertEqual((source["url"], source["title"]), (url, "Fixture Documentation"))
        self.assertIn("captured from browser", source["excerpt"])
        self.assertRegex(source["fetched_at"], r"\+00:00$")
        with self.assertRaisesRegex(SourceError, "已跳转"):
            capture_sources(self.root, self.config(), ["https://example.test/redirect"])
        with self.assertRaisesRegex(SourceError, "打开来源失败"):
            capture_sources(self.root, self.config("allowed.test"), [url])

    def test_source_url_excludes_nonweb_schemes_and_credentials(self):
        for value in ("file:///etc/passwd", "https://user:key@example.test/", "https://[bad", "https://x.test/a b"):
            with self.subTest(value=value), self.assertRaises(SourceError):
                _host(value)
