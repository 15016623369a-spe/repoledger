"""Run the twelve-question, three-arm holdout with the frozen V2 policy."""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
from itertools import permutations
from pathlib import Path
import random
import tempfile

import wiki_strategy as strategy
from wiki_holdout_cases import CASES
from wiki_net_cases import LOCATE, TRACE, IMPACT

base = strategy.base
pilot = strategy.pilot
ARMS = ("without_wiki", *strategy.ARMS)
CATEGORIES = (LOCATE, TRACE, IMPACT)
ORDER_SEED = 261127


def schedule():
    orders = list(permutations(ARMS)) * 2
    random.Random(ORDER_SEED).shuffle(orders)
    return [{"id": case["id"], "order": list(order)}
            for case, order in zip(CASES, orders)]


def experiment(baseline: Path, repo: Path) -> dict:
    _, conditions = strategy.baseline_conditions(baseline, repo)
    old_questions = {case["question"] for case in (*pilot.CASES, *base.FIXTURES["qa"])}
    if (len({case["id"] for case in CASES}) != 12
            or len({case["question"] for case in CASES}) != 12
            or Counter(case["category"] for case in CASES) != dict.fromkeys(CATEGORIES, 4)):
        raise RuntimeError("The holdout must contain twelve distinct questions, four per category")
    for case in CASES:
        if case["question"] in old_questions or case["fields"].keys() != case["expected"].keys():
            raise RuntimeError(f'{case["id"]}: repeated question or invalid answer fields')
        for ref in case["evidence"]:
            if ref["needle"] not in (repo / ref["file"]).read_text(encoding="utf-8"):
                raise RuntimeError(f'{case["id"]}: source evidence changed in {ref["file"]}')
    return conditions | {
        "design": "unseen_repository_qa_holdout",
        "schedule": schedule(), "order_seed": ORDER_SEED,
        "strategy_version": "v2", "strategy": strategy.STRATEGIES["v2"], "trace_source": True,
        "files": conditions["files"] | {
            name: pilot.digest(base.HERE / name)
            for name in ("wiki_holdout.py", "wiki_holdout_cases.py")
        },
    }


def summarize(rows):
    complete, usage_count, arms = strategy.summarize_arms(rows, ARMS)
    for arm in ARMS:
        arms[arm]["p90_seconds"] = pilot.percentile([row[arm]["seconds"] for row in complete], .90)

    comparisons = {}
    for name, baseline, treatment in (("wiki_vs_no_wiki", *ARMS[:2]), ("v2_vs_wiki", *ARMS[1:])):
        wins = [row["id"] for row in complete if row[treatment]["passed"] and not row[baseline]["passed"]]
        losses = [row["id"] for row in complete if row[baseline]["passed"] and not row[treatment]["passed"]]
        comparisons[name] = {"treatment_only": wins, "baseline_only": losses,
                             "net_successes": len(wins) - len(losses)}

    gate = "pending"
    if len(complete) == len(CASES):
        ordinary, candidate = (arms[arm] for arm in ARMS[1:])
        if candidate["passed"] < ordinary["passed"] or candidate["budget_stops"] > ordinary["budget_stops"]:
            gate = "not_met"
        elif candidate["passed"] > ordinary["passed"]:
            gate = "met_on_holdout"
        elif usage_count != len(complete):
            gate = "token_usage_unverified"
        else:
            cost_fields = ("source_calls", "matched_usage_tokens")
            changes = [candidate[key] - ordinary[key] for key in cost_fields]
            gate = "met_on_holdout" if max(changes) <= 0 and min(changes) < 0 else "not_met"

    categories = {case["id"]: case["category"] for case in CASES}
    return {
        "complete_tasks": len(complete), "planned_tasks": len(CASES),
        "all_arms_usage_count": usage_count, "arms": arms, "comparisons": comparisons,
        "v2_gate": gate,
        "passes_by_category": {
            category: {arm: sum(row[arm]["passed"] for row in complete
                                if categories[row["id"]] == category) for arm in ARMS}
            for category in CATEGORIES
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--baseline", type=Path, default=strategy.default_baseline())
    parser.add_argument("--source", type=Path, default=base.ROOT)
    parser.add_argument("--bundle", type=Path, default=base.DEFAULT_BUNDLE)
    parser.add_argument("--output", type=Path, default=base.ROOT / "runs/wiki-strategy-holdout")
    parser.add_argument("--claude", default="claude")
    args = parser.parse_args()
    args.baseline = args.baseline.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="wiki-holdout-") as tmp:
            repo = Path(tmp) / "source"
            base.snapshot(args.source.resolve(), repo, args.bundle.resolve())
            manifest = experiment(args.baseline, repo)
            if args.verify:
                print(f"Verified {len(manifest['schedule'])} fresh questions, fixed source and Wiki.")
                return 0
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=True)
            with (output / ".run-lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                args.resume = (output / "report.json").is_file()
                return strategy.run_comparison(args, output, repo, manifest,
                                               {case["id"]: case for case in CASES},
                                               summarize_results=summarize)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Wiki holdout blocked: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
