"""Regression coverage for applying a frozen strategy version to C alone."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wiki_strategy as strategy


class StrategyTest(unittest.TestCase):
    def test_candidate_receives_the_frozen_v2_policy(self):
        candidate = strategy.STRATEGIES["v2"]
        self.assertNotEqual(candidate, strategy.STRATEGIES["v1"])
        manifest = {
            "strategy_version": "v2", "strategy": candidate,
            "revision": "repo-revision", "wiki_pages": {},
            "requested_model": "same-model", "actual_model": "same-model",
            "schedule": [{"id": "W02", "order": list(strategy.ARMS)}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(resume=False, baseline=root, claude="fake-claude")
            row = {"status": "ok", "passed": True, "seconds": 1.0,
                   "mcp_connected": True, "calls": {"Grep": 1}}
            probe = {"status": "ok", "answer": "EVAL_READY", "model": "same-model"}
            with (patch.object(strategy.shutil, "copytree"),
                  patch.object(strategy.shutil, "which", return_value="fake-claude"),
                  patch.object(strategy.pilot, "wiki_manifest", return_value={}),
                  patch.object(strategy.base, "check_endpoint", return_value=None),
                  patch.object(strategy.base, "resolve_provider", return_value=({}, "fixture")),
                  patch.object(strategy.base, "claude", return_value=probe),
                  patch.object(strategy.pilot, "attempt", return_value=row) as attempt,
                  patch("builtins.print")):
                self.assertEqual(strategy.run_comparison(
                    args, root, root / "source", manifest, {"W02": {}},
                ), 0)

            self.assertEqual([call.kwargs["system_append"] for call in attempt.call_args_list],
                             [None, candidate])
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["experiment"]["strategy_version"], "v2")
            self.assertEqual(report["summary"]["arms"]["wiki_with_strategy"]["source_calls"], 1)

    def test_finished_v1_report_remains_readable_with_new_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"experiment": {"strategy": strategy.STRATEGIES["v1"]},
                     "finished_at": "finished", "summary": {}}
            (root / "report.json").write_text(json.dumps(state), encoding="utf-8")
            args = SimpleNamespace(resume=True)
            with patch("builtins.print"):
                self.assertEqual(strategy.run_comparison(args, root, root, {"strategy_version": "v1"}, {}), 0)
                with self.assertRaisesRegex(RuntimeError, "strategy version"):
                    strategy.run_comparison(args, root, root, {"strategy_version": "v2"}, {})
