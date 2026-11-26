"""Regression coverage for the three-arm holdout controls."""

from collections import Counter
from itertools import permutations
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wiki_holdout as holdout
import wiki_strategy as strategy


class HoldoutTest(unittest.TestCase):
    def test_every_arm_order_occurs_twice(self):
        schedule = holdout.schedule()
        self.assertEqual(len(schedule), 12)
        self.assertEqual(Counter(tuple(item["order"]) for item in schedule),
                         dict.fromkeys(permutations(holdout.ARMS), 2))
        self.assertEqual(len({item["id"] for item in schedule}), 12)

    def test_same_question_dispatches_all_three_arms(self):
        arms = holdout.ARMS
        manifest = {
            "strategy_version": "v2", "strategy": strategy.STRATEGIES["v2"],
            "design": "unseen_repository_qa_holdout",
            "revision": "fixed", "wiki_pages": {}, "requested_model": "fixture",
            "actual_model": "fixture", "schedule": [{"id": "H01", "order": list(arms)}],
        }
        row = {"status": "ok", "passed": True, "seconds": 1.0}
        calls = [row | {"mcp_connected": index > 0} for index in range(3)]
        probe = {"status": "ok", "answer": "EVAL_READY", "model": "fixture"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(resume=False, baseline=root, claude="fixture")
            with (patch.object(strategy.shutil, "copytree"),
                  patch.object(strategy.shutil, "which", return_value="fixture"),
                  patch.object(strategy.pilot, "wiki_manifest", return_value={}),
                  patch.object(strategy.base, "check_endpoint", return_value=None),
                  patch.object(strategy.base, "resolve_provider", return_value=({}, "fixture")),
                  patch.object(strategy.base, "claude", return_value=probe),
                  patch.object(strategy.pilot, "attempt", side_effect=calls) as attempt,
                  patch("builtins.print")):
                self.assertEqual(strategy.run_comparison(
                    args, root, root / "source", manifest, {"H01": holdout.CASES[0]},
                    summarize_results=holdout.summarize,
                ), 0)
            self.assertEqual([call.args[4] for call in attempt.call_args_list],
                             ["without_wiki", "with_wiki", "with_wiki"])
            self.assertEqual([call.kwargs["system_append"] for call in attempt.call_args_list],
                             [None, None, manifest["strategy"]])
            result = strategy.pilot.read_json(root / "report.json")
            self.assertEqual(result["summary"]["complete_tasks"], 1)
            self.assertEqual(result["summary"]["comparisons"]["wiki_vs_no_wiki"]["net_successes"], 0)
