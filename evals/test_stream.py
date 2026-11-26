"""Regression tests for Claude Code failure diagnostics in paired evaluations."""

import json
from pathlib import Path
import tempfile
import unittest

from benchmark import WIKI_TOOLS, parse_stream


def stream(*packets):
    return "\n".join(json.dumps(packet) for packet in packets)


def assistant(model, text):
    return {"type": "assistant", "message": {
        "model": model, "content": [{"type": "text", "text": text}],
    }}


class StreamTest(unittest.TestCase):
    def test_source_trace_records_search_modes_and_actual_read_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "agent" / "usage.py"
            file.parent.mkdir()
            file.touch()
            key = "fixture-api-key"
            rows = parse_stream(stream(
                {"type": "assistant", "message": {"id": "round-1", "model": "actual-model", "content": [
                    {"type": "tool_use", "id": "a", "name": "Grep", "input": {
                        "pattern": f"cache_hit|{key}", "path": str(root / "agent"),
                        "glob": "*.py", "output_mode": "content",
                    }},
                ]}},
                {"type": "assistant", "message": {"id": "round-1", "model": "actual-model", "content": [
                    {"type": "tool_use", "id": "b", "name": "Grep", "input": {"pattern": "cache_hit"}},
                ]}},
                {"type": "user", "tool_use_result": {
                    "mode": "content", "filenames": [], "numLines": 1,
                    "content": f"{file}:132:def hidden_source_line():",
                }, "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}},
                {"type": "user", "tool_use_result": {
                    "mode": "files_with_matches", "filenames": [str(file)], "totalFiles": 3,
                }, "message": {"content": [{"type": "tool_result", "tool_use_id": "b"}]}},
                {"type": "assistant", "message": {"id": "round-2", "model": "actual-model", "content": [
                    {"type": "tool_use", "id": "c", "name": "Read", "input": {
                        "file_path": str(file), "offset": 130, "limit": 80,
                    }},
                    {"type": "tool_use", "id": "d", "name": "Grep", "input": {
                        "path": str(file), "pattern": "cache_hit", "output_mode": "content",
                    }},
                ]}},
                {"type": "user", "tool_use_result": {"type": "text", "file": {
                    "filePath": str(file), "content": "hidden_source_line", "startLine": 130, "numLines": 5,
                }}, "message": {"content": [{"type": "tool_result", "tool_use_id": "c"}]}},
                {"type": "user", "tool_use_result": {
                    "mode": "content", "numLines": 1, "content": "132:hidden_source_line",
                }, "message": {"content": [{"type": "tool_result", "tool_use_id": "d"}]}},
                {"type": "result", "subtype": "success", "result": "{}"},
            ), "", 0, trace_tools=True, trace_root=root, trace_secrets=(key,))["tool_trace"]

        first, second, read, file_search = rows
        self.assertEqual([row["turn"] for row in rows], [1, 1, 2, 2])
        self.assertEqual(first["request"], {
            "pattern": "cache_hit|[redacted]", "path": "agent", "glob": "*.py", "output_mode": "content",
        })
        self.assertEqual(first["result"]["files"], ["agent/usage.py"])
        self.assertEqual(first["result"]["locations"], [{"file": "agent/usage.py", "line": 132}])
        self.assertEqual(second["request"]["path"], ".")
        self.assertEqual(second["result"]["total_files"], 3)
        self.assertTrue(second["result"]["files_truncated"])
        self.assertEqual(read["request"], {"file": "agent/usage.py", "offset": 130, "limit": 80})
        self.assertEqual(read["result"], {"file": "agent/usage.py", "line_start": 130, "line_end": 134})
        self.assertEqual(file_search["result"]["files"], ["agent/usage.py"])
        self.assertEqual(file_search["result"]["locations"], [{"file": "agent/usage.py", "line": 132}])
        self.assertNotIn("hidden_source_line", json.dumps(rows))
        self.assertNotIn(key, json.dumps(rows))

    def test_trace_retains_order_and_empty_wiki_result_without_raw_arguments(self):
        row = parse_stream(stream(
            {"type": "assistant", "message": {"model": "actual-model", "content": [
                {"type": "tool_use", "id": "a", "name": WIKI_TOOLS[1], "input": {"query": "private"}},
                {"type": "tool_use", "id": "b", "name": "Grep", "input": {"pattern": "private"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "private"},
                {"type": "tool_result", "tool_use_id": "a", "content": [
                    {"type": "text", "text": '{"matches":[]}'},
                ]},
            ]}},
            {"type": "result", "subtype": "success", "result": "{}"},
        ), "", 0, trace_tools=True)
        self.assertEqual(row["tool_trace"], [
            {"tool": WIKI_TOOLS[1], "outcome": "ok", "matches": 0},
            {"tool": "Grep", "outcome": "error"},
        ])
        self.assertNotIn("private", json.dumps(row))

    def test_synthetic_provider_error_is_not_a_model(self):
        row = parse_stream(stream(
            {"type": "system", "subtype": "init", "model": "configured-model[1m]"},
            assistant("<synthetic>", "API Error 402: insufficient credit balance"),
            {"type": "result", "subtype": "success", "is_error": True},
        ), "", 1)
        self.assertIsNone(row["model"])
        self.assertEqual(row["configured_model"], "configured-model[1m]")
        self.assertEqual(row["status"], "billing")
        self.assertTrue(row["provider_interruption"])
        self.assertNotIn("insufficient credit", json.dumps(row))

    def test_provider_error_does_not_overwrite_a_real_response_model(self):
        row = parse_stream(stream(
            assistant("actual-model", "Earlier response"),
            assistant("<synthetic>", "API Error: rate limit reached"),
            {"type": "result", "subtype": "success", "is_error": True},
        ), "", 1)
        self.assertEqual(row["model"], "actual-model")
        self.assertEqual(row["status"], "rate_limit")
        self.assertTrue(row["provider_interruption"])

    def test_turn_limit_remains_an_evaluated_failure(self):
        row = parse_stream(stream(
            assistant("actual-model", "Earlier response"),
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
        ), "", 1)
        self.assertEqual(row["status"], "budget")
        self.assertEqual(row["limit"], "error_max_turns")
        self.assertFalse(row["provider_interruption"])
