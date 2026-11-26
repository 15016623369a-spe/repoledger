"""从真实 Git fixture 验证 Wiki 更新、审批和重新打开日志。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent.llm import LLMResponse, ProtocolCheckingProvider
from agent.loop import Harness, HarnessConfig
from agent.wiki import WikiProvider, plan_wiki, wiki_registry


class TestRepositoryWiki(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runs = self.root / "runs"
        self.wiki = self.repo / "wiki"
        self.git("init", "-q")
        (self.repo / "README.md").write_text("# Project\n\n## Architecture\n\nOverview.\n")
        (self.repo / "service.py").write_text("def run():\n    return True\n")
        (self.repo / "requirements.txt").write_text("example-lib==1.0\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "init")
        (self.repo / "AGENTS.md").write_text("Existing user instructions\n")
        (self.repo / "private.env").write_text("Untracked file\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def cli(self, *args, code=0):
        proc = subprocess.run([sys.executable, "-m", "agent", "--runs", str(self.runs), *args],
                              capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(proc.returncode, code, proc.stderr + proc.stdout)
        return proc.stdout

    def approve_draft(self, run):
        approval = self.cli("approvals", "list", run)
        number, details = approval.strip().split(" ", 1)
        request = json.loads(details)
        self.assertIn(str(self.wiki / "AGENTS.md"), request["detail"])
        self.cli("approvals", "approve", run, number)
        return self.cli("resume", run)

    def test_wiki_pages_hashes_and_draft_approval(self):
        self.cli("wiki", str(self.repo), "--run-id", "first", code=1)
        self.assertEqual({p.name for p in self.wiki.iterdir() if p.suffix == ".md"},
                         {"architecture.md", "modules.md", "decisions.md", "dependencies.md"})
        deps = (self.wiki / "dependencies.md").read_text()
        self.assertIn("example-lib==1.0", deps)
        modules = (self.wiki / "modules.md").read_text()
        digest = hashlib.sha256((self.repo / "service.py").read_bytes()).hexdigest()
        self.assertIn(digest, modules)
        self.assertIn("run", modules)
        self.assertNotIn("private.env", modules)
        self.assertFalse((self.wiki / "AGENTS.md").exists())
        self.approve_draft("first")
        self.assertTrue((self.wiki / "AGENTS.md").is_file())
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "Existing user instructions\n")
        self.assertIn("write_wiki_page", self.cli("replay", "first", "--usage"))

    def test_only_affected_page_updates_and_dirty_snapshot_can_revert(self):
        self.cli("wiki", str(self.repo), "--run-id", "first", code=1)
        self.approve_draft("first")
        original_times = {p.name: p.stat().st_mtime_ns for p in self.wiki.glob("*.md")}
        self.cli("wiki", str(self.repo), "--run-id", "noop")
        self.assertEqual(original_times, {p.name: p.stat().st_mtime_ns for p in self.wiki.glob("*.md")})

        (self.repo / "service.py").write_text("def revised():\n    return False\n")
        self.cli("wiki", str(self.repo), "--run-id", "dirty")
        changed_times = {p.name: p.stat().st_mtime_ns for p in self.wiki.glob("*.md")}
        self.assertEqual({name for name in changed_times if changed_times[name] != original_times[name]},
                         {"modules.md"})
        self.assertIn("revised", (self.wiki / "modules.md").read_text())
        self.git("restore", "service.py")
        self.cli("wiki", str(self.repo), "--run-id", "restore")
        self.assertNotIn("revised", (self.wiki / "modules.md").read_text())

        self.git("mv", "service.py", "renamed.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "move")
        self.cli("wiki", str(self.repo), "--run-id", "renamed")
        content = (self.wiki / "modules.md").read_text()
        self.assertIn("renamed.py", content)
        self.assertNotIn("service.py", content)

    def test_denied_draft_and_unmanaged_output_are_preserved(self):
        self.cli("wiki", str(self.repo), "--run-id", "deny", code=1)
        selector = self.cli("approvals", "list", "deny").split()[0]
        self.cli("approvals", "deny", "deny", selector)
        self.assertIn("未获批准", self.cli("resume", "deny"))
        self.assertFalse((self.wiki / "AGENTS.md").exists())

        manual = "Hand written"
        (self.wiki / "architecture.md").write_text(manual)
        self.assertFalse((self.runs / "unmanaged" / "events.jsonl").exists())
        self.cli("wiki", str(self.repo), "--run-id", "unmanaged", code=2)
        self.assertEqual((self.wiki / "architecture.md").read_text(), manual)

    def test_recorded_plan_rejects_source_changes_before_approved_write(self):
        self.cli("wiki", str(self.repo), "--run-id", "stale", code=1)
        selector = self.cli("approvals", "list", "stale").split()[0]
        self.cli("approvals", "approve", "stale", selector)
        (self.repo / "README.md").write_text("# Changed while waiting\n")
        self.assertIn("源文件已变化", self.cli("resume", "stale", code=1))
        self.assertFalse((self.wiki / "AGENTS.md").exists())

    def test_web_sources_update_only_dependencies_and_remain_pinned(self):
        self.cli("wiki", str(self.repo), "--run-id", "first", code=1)
        self.approve_draft("first")
        pages = list(self.wiki.glob("*.md"))
        before = {p.name: p.stat().st_mtime_ns for p in pages}
        manifest = self.root / "sources.json"
        url = "https://example.test/official/overview"
        timestamp = "2026-11-25T16:14:08+00:00"
        manifest.write_text(json.dumps([{
            "url": url, "title": "Official documentation", "fetched_at": timestamp,
            "excerpt": "Verified source excerpt",
        }]), encoding="utf-8")
        self.cli("wiki", str(self.repo), "--run-id", "verify", "--web-sources", str(manifest))
        self.assertEqual([p.name for p in pages if p.stat().st_mtime_ns != before[p.name]],
                         ["dependencies.md"])
        body = (self.wiki / "dependencies.md").read_text()
        self.assertIn(f"<{url}> · 抓取于 {timestamp}", body)
        self.assertIn("Verified source excerpt", body)
        self.assertIn("来源未变化", self.cli("wiki", str(self.repo), "--run-id", "no-recapture"))
        self.assertEqual((self.wiki / "dependencies.md").read_text(), body)

    def test_optional_model_writes_only_prose_and_usage_is_logged(self):
        root, plan = plan_wiki(self.repo, provider="openai", model="fake")
        seen = []
        class Author:
            name = "test-author"

            def complete(self, messages, tools, *, temperature=0.0):
                seen.append(messages)
                if tools:
                    raise AssertionError("Wiki author must have no tools")
                return LLMResponse(text="# 项目架构\n\n生成的正文", usage={"prompt_tokens": 17, "completion_tokens": 5})

        cfg = HarnessConfig(root, self.runs, async_approval=True)
        workflow = ProtocolCheckingProvider(WikiProvider(root, plan, author=Author()))
        result = Harness(provider=workflow, registry=wiki_registry(plan), config=cfg).run("test", run_id="model")
        self.assertEqual(result.reason, "approval_pending")
        self.assertEqual(len(seen), 5)
        self.assertEqual(result.stats.tokens_in, 85)
        self.assertEqual(result.stats.tokens_out, 25)
        page = (self.wiki / "architecture.md").read_text()
        self.assertIn("生成的正文", page)
        self.assertEqual(page.count("# 项目架构"), 1)
