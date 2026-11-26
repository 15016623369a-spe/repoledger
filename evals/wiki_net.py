"""Run the preregistered 40-task production Wiki A/B pilot."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import benchmark as base
from wiki_net_cases import CASES, LOCATE, TRACE, IMPACT, REPAIR

sys.path.insert(0, str(base.ROOT))
from agent.llm import OpenAICompatProvider
from agent.usage import collect, percentile
from agent.wiki import _previous
from agent.wiki_mcp import PAGES, WikiTools

ARMS = ("without_wiki", "with_wiki")
CATEGORIES = (LOCATE, TRACE, IMPACT, REPAIR)
WIKI_PAGES = PAGES.keys() - {"AGENTS.md"}
SEED = 261126
REGISTRATION = {
    "tasks_per_category": 10, "seed": SEED, "timeout_seconds": 120,
    "quality_gain_tasks": 4, "allowed_cost_ratio": 1.10, "allowed_p90_ratio": 1.10,
    "equal_quality_cost_ratio": 0.85, "wiki_generation_share": 1.0,
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints():
    return {name: digest(base.HERE / name)
            for name in ("wiki_net.py", "wiki_net_cases.py", "benchmark.py")}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(repo):
    if Counter(case["category"] for case in CASES) != dict.fromkeys(CATEGORIES, 10):
        raise RuntimeError("Expected four strata of ten cases")
    if len({case["id"] for case in CASES}) != len(CASES):
        raise RuntimeError("Duplicate case ID")
    fixtures = []
    for case in CASES:
        if case["category"] != REPAIR:
            if set(case["expected"]) != set(case["fields"]) or not case["evidence"]:
                raise RuntimeError(f'{case["id"]}: invalid answer key')
            for ref in case["evidence"]:
                if ref["needle"] not in (repo / ref["file"]).read_text(encoding="utf-8"):
                    raise RuntimeError(f'{case["id"]}: source evidence changed in {ref["file"]}')
            continue
        implementation = repo / case["file"]
        original = implementation.read_text(encoding="utf-8")
        try:
            base.seed_bug(repo, case)
        finally:
            implementation.write_text(original, encoding="utf-8")
        if base.test_bug(repo, case).returncode:
            raise RuntimeError(f'{case["id"]}: restoring the baseline did not pass')
        fixtures.append(case["id"])
    if base.git(repo, "status", "--porcelain"):
        raise RuntimeError("Fixture validation left the source dirty")
    return fixtures


def schedule():
    rng = random.Random(SEED)
    plan = []
    for category in CATEGORIES:
        sequence = [ARMS, ARMS[::-1]] * 5
        rng.shuffle(sequence)
        plan.extend({"id": case["id"], "category": category, "order": order}
                    for case, order in zip((c for c in CASES if c["category"] == category), sequence))
    rng.shuffle(plan)
    return plan


def freeze_wiki(repo, workspace, output, revision):
    archive = output / "wiki-snapshot"
    author = OpenAICompatProvider()
    if not author.api_key:
        raise RuntimeError("Wiki author credentials are missing from the project provider configuration")
    env = {**base.TEST_ENV, "HARNESS_BASE_URL": author.base_url,
           "HARNESS_MODEL": author.model, "HARNESS_API_KEY": author.api_key}
    runs = output / "wiki-author-runs"
    base.snapshot(repo, workspace, None)
    run_id = datetime.now(timezone.utc).strftime("wiki-%Y%m%d-%H%M%S-%f")
    command = [sys.executable, "-m", "agent", "--runs", str(runs)]
    started_at = utcnow()
    try:
        result = base.run([*command, "wiki", ".", "--provider", "openai", "--run-id", run_id],
                          workspace, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Wiki generation timed out") from None
    pending = base.run([*command, "approvals", "list", run_id], workspace, env=env)
    if result.returncode != 1 or pending.returncode or not pending.stdout[:1].isdigit():
        raise RuntimeError("Real Wiki generation did not reach the draft approval boundary")
    selector = pending.stdout.split(maxsplit=1)[0]
    denied = base.run([*command, "approvals", "deny", run_id, selector], workspace, env=env)
    if denied.returncode or base.run([*command, "resume", run_id], workspace, env=env).returncode:
        raise RuntimeError("Wiki generation could not finalize the evaluation snapshot")
    manifest = wiki_manifest(workspace, repo, revision)
    usage = collect(runs / run_id)
    snapshot = {"provider": "openai", "model": author.model, "pages": manifest, "run_id": run_id,
                "started_at": started_at, "finished_at": utcnow(),
                "input_tokens": usage.tokens_in, "cache_read_tokens": usage.cache_hit_tokens,
                "output_tokens": usage.tokens_out, "seconds": usage.wall_s}
    with tempfile.TemporaryDirectory(prefix="wiki-snapshot-", dir=output) as directory:
        staged = Path(directory) / "snapshot"
        shutil.copytree(workspace / "wiki", staged / "wiki")
        base.save(staged / "manifest.json", snapshot)
        staged.rename(archive)
    return snapshot


def wiki_manifest(wiki, repo, revision):
    pages = WikiTools(wiki).list_pages()
    if {page["page"] for page in pages} != WIKI_PAGES:
        raise RuntimeError("The Wiki snapshot must contain exactly the four queryable pages")
    recorded = {}
    for name in sorted(WIKI_PAGES):
        path = wiki / "wiki" / name
        metadata, checksum = _previous(path)
        if (metadata.get("head") != revision or metadata.get("provider") != "openai"
                or metadata.get("dirty") or not metadata.get("model") or not metadata["sources"]):
            raise RuntimeError(f"{name}: invalid model provenance or source revision")
        for source, expected in metadata["sources"].items():
            path = (repo / source).resolve()
            if not path.is_relative_to(repo.resolve()) or digest(path) != expected:
                raise RuntimeError(f"{name}: source hashes differ from the pinned commit")
        recorded[name] = checksum
    return recorded


def factual_score(case, reply):
    # Accept an unambiguous JSON object surrounded by prose or fences for the
    # diagnostic metric only. Strict pass still uses benchmark.grade.
    decoder = json.JSONDecoder()
    candidates = []
    for offset, character in enumerate(reply):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(reply, offset)
        except ValueError:
            continue
        if isinstance(value, dict) and set(case["expected"]) <= value.keys():
            candidates.append(value)
    if len(candidates) != 1:
        return None
    values = candidates[0]
    for key, expected in case["expected"].items():
        value = values[key]
        if isinstance(value, str) and type(expected) in (int, bool):
            try:
                value = json.loads(value.strip().lower())
            except ValueError:
                pass
        if type(value) is not type(expected) or base.scalar(value) != base.scalar(expected):
            return False
    return True


def repair(args, source, wiki, case, arm, output):
    with tempfile.TemporaryDirectory(prefix="wiki-net-edit-") as directory:
        root = Path(directory)
        repo, grader = root / "work", root / "grader"
        base.snapshot(source, repo, None)
        file = case["file"]
        implementation = repo / file
        original = implementation.read_text(encoding="utf-8")
        seeded = original.replace(case["anchor"], case["mutation"], 1)
        implementation.write_text(seeded, encoding="utf-8")
        row = base.claude(args, repo, case["question"] +
                          "\n请定位并修复问题。只修改必要的实现，不修改测试或文档。",
                          wiki=wiki, edit=True)
        row["passed"] = False
        if row["status"] != "ok":
            return row
        changes = set(base.git(repo, "status", "--porcelain", "-z").split("\0")) - {""}
        if changes - {" M " + file} or implementation.is_symlink() or not implementation.is_file():
            return row | {"grading": "invalid_change_scope"}
        candidate = implementation.read_text(encoding="utf-8")
        if candidate == seeded:
            return row | {"grading": "no_repair"}
        patch = difflib.unified_diff(seeded.splitlines(keepends=True), candidate.splitlines(keepends=True),
                                     fromfile=f"a/{file}", tofile=f"b/{file}")
        (output / f'{case["id"]}-{arm}.diff').write_text("".join(patch), encoding="utf-8")
        base.snapshot(source, grader, None)
        (grader / file).write_text(candidate, encoding="utf-8")
        try:
            focused = base.test_bug(grader, case).returncode == 0
            full_suite = focused and base.run(
                [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests"],
                grader, timeout=90, env=base.TEST_ENV).returncode == 0
        except subprocess.TimeoutExpired:
            return row | {"grading": "grader_timeout"}
        return row | {"focused_pass": focused, "suite_pass": full_suite,
                      "passed": focused and full_suite}


def attempt(args, repo, archive, case, arm, output, model, *,
            system_append=None, trace_tools=False, trace_source=False):
    wiki = archive if arm == "with_wiki" else None
    if case["category"] == REPAIR:
        row = repair(args, repo, wiki, case, arm, output)
    else:
        row = base.claude(args, repo, base.qa_prompt(case), wiki=wiki,
                          system_append=system_append, trace_tools=trace_tools, trace_source=trace_source)
        if row["status"] == "ok":
            strict, wrong = base.grade(case, row["answer"])
            values = base.answer_object(row["answer"])
            expected = case["expected"]
            format_ok = values is not None and values.keys() == expected.keys() and all(
                type(values[key]) is type(value) for key, value in expected.items())
            row |= {"passed": strict and format_ok, "wrong_fields": wrong, "format_ok": format_ok,
                    "fact_match": factual_score(case, row["answer"])}
    row.setdefault("passed", False)
    actual = row.get("model")
    if actual != model and (actual is not None or row["status"] == "ok"):
        row |= {"status": "model_mismatch", "passed": False}
    if (wiki is not None and row["status"] == "ok" and not row.get("mcp_connected")
            or wiki is None and (row.get("mcp_connected") or any(
                name in base.WIKI_TOOLS for name in row.get("calls", {})))):
        row |= {"status": "mcp_configuration_mismatch", "passed": False}
    return row


def paired_interval(pairs):
    groups = [[int(p["with_wiki"]["passed"]) - int(p["without_wiki"]["passed"])
               for p in pairs if p["category"] == category] for category in CATEGORIES]
    rng = random.Random(SEED)
    sample = sorted(100 * sum(sum(rng.choices(group, k=len(group))) for group in groups) / len(pairs)
                    for _ in range(5000))
    return [round(sample[125], 2), round(sample[4874], 2)]


def paired_counts(pairs):
    return {
        "wiki_only": sum(p["with_wiki"]["passed"] and not p["without_wiki"]["passed"] for p in pairs),
        "baseline_only": sum(p["without_wiki"]["passed"] and not p["with_wiki"]["passed"] for p in pairs),
        "both": sum(p["with_wiki"]["passed"] and p["without_wiki"]["passed"] for p in pairs),
        "neither": sum(not p["with_wiki"]["passed"] and not p["without_wiki"]["passed"] for p in pairs),
    }


def valid_amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def summarize(report, billing=None):
    rows = report["results"]
    planned = len(report["schedule"])
    pairs = [p for p in rows if all(arm in p for arm in ARMS)]
    complete = len(pairs) == planned
    mismatches = {"model_mismatch", "mcp_configuration_mismatch"}
    invalid = any(p[arm]["status"] in mismatches or p[arm].get("grading") == "grader_timeout"
                  for p in rows for arm in ARMS if arm in p)
    by_arm = {}
    for arm in ARMS:
        attempts = [p[arm] for p in rows if arm in p]
        passed = sum(a["passed"] for a in attempts)
        times = [a["seconds"] for a in attempts]
        counts = Counter(a["status"] for a in attempts)
        costs = [a.get("reported_cost_usd") for a in attempts]
        have_cost = [value for value in costs if valid_amount(value)]
        tokens = [a for a in attempts if a.get("usage")]
        billed = None
        if billing is not None and complete:
            billed = billing[arm] + (billing["wiki_generation_and_updates"] *
                                     REGISTRATION["wiki_generation_share"] if arm == "with_wiki" else 0)
        by_arm[arm] = {
            "attempted": len(attempts), "passed": passed, "end_to_end_pct": 100 * passed / planned,
            "fact_matches": sum(a.get("fact_match") is True for a in attempts),
            "fact_mismatches": sum(a.get("fact_match") is False for a in attempts),
            "fact_unresolved": sum("fact_match" in a and a["fact_match"] is None for a in attempts),
            "format_passes": sum(a.get("format_ok") is True for a in attempts),
            "format_fail_but_fact_match": sum(a.get("fact_match") is True and a.get("format_ok") is False
                                              for a in attempts),
            "runtime_statuses": dict(counts),
            "timeout_pct": 100 * counts["timeout"] / len(attempts) if attempts else None,
            "wiki_used": sum(any(name in base.WIKI_TOOLS for name in a.get("calls", {})) for a in attempts),
            "seconds_median": statistics.median(times) if times else None,
            "seconds_p90": percentile(times, 0.90),
            "usage_reported_attempts": len(tokens),
            "input_tokens_observed": sum(a["input_tokens"] for a in tokens),
            "output_tokens_observed": sum(a["output_tokens"] for a in tokens),
            "claude_estimated_cost_observed_usd": sum(have_cost) if have_cost else None,
            "estimated_cost_reported_attempts": len(have_cost),
            "actual_billed_total": billed,
            "billed_cost_per_success": billed / passed if billed is not None and passed else None,
        }

    baseline, treatment = [by_arm[arm] for arm in ARMS]
    delta = treatment["passed"] - baseline["passed"] if complete else None
    latency = (treatment["seconds_p90"] / baseline["seconds_p90"]
               if complete and baseline["seconds_p90"] else None)
    cost = (treatment["billed_cost_per_success"] / baseline["billed_cost_per_success"]
            if complete and baseline["billed_cost_per_success"]
            and treatment["billed_cost_per_success"] is not None else None)
    conclusion = "incomplete"
    if complete:
        quality = delta >= REGISTRATION["quality_gain_tasks"]
        efficiency = delta == 0
        if invalid:
            conclusion = "configuration_or_grader_error"
        elif not (quality or efficiency) or latency is None or latency > REGISTRATION["allowed_p90_ratio"]:
            conclusion = "threshold_not_met"
        elif cost is None:
            conclusion = "cost_unverified"
        elif cost <= (REGISTRATION["allowed_cost_ratio"] if quality
                      else REGISTRATION["equal_quality_cost_ratio"]):
            conclusion = "pilot_threshold_met"
        else:
            conclusion = "threshold_not_met"
    return {"planned_per_arm": planned, "pairs_completed": len(pairs), "arms": by_arm,
            "net_successes": delta, "p90_ratio": latency, "actual_cost_per_success_ratio": cost,
            "paired_counts": paired_counts(pairs),
            "by_category": {kind: paired_counts([p for p in pairs if p["category"] == kind])
                            for kind in CATEGORIES},
            "paired_bootstrap_95pct_points": paired_interval(pairs) if complete and not invalid else None,
            "billing_currency": billing["currency"] if billing else None,
            "conclusion": conclusion}


def load_billing(path):
    bill = read_json(path)
    fees = (*ARMS, "wiki_generation_and_updates")
    if (not isinstance(bill, dict) or not all(valid_amount(bill.get(key)) for key in fees)
            or not isinstance(bill.get("currency"), str) or not bill["currency"].strip()
            or not isinstance(bill.get("source"), str) or not bill["source"].strip()):
        raise RuntimeError("Billing needs currency, source, both arm totals and Wiki maintenance total")
    return bill


def save_blind_reviews(report, output):
    cases = {case["id"]: case for case in CASES}
    entries = []
    for pair in report["results"]:
        case = cases[pair["id"]]
        if case["category"] == REPAIR:
            continue
        for arm in ARMS:
            row = pair.get(arm, {})
            if row.get("status") == "ok":
                anon = hashlib.sha256(f'{report["started_at"]}/{case["id"]}/{arm}'.encode()).hexdigest()[:12]
                entries.append({"id": anon, "task": case["id"], "question": case["question"],
                                "key": case["expected"], "answer": row["answer"]})
                row["blind_review_id"] = anon
    random.Random(SEED).shuffle(entries)
    base.save(output / "blind-review.json", entries)


def prepare(args, repo):
    base.snapshot(args.source.resolve(), repo, args.bundle.resolve())
    return validate(repo)


def restore_interrupted_pilot(source, output, repo, report):
    raw = (source / "report.json").read_bytes()
    original = json.loads(raw)
    plan = json.loads(json.dumps(report["schedule"]))
    if (not original.get("finished_at") or original["schedule"] != plan
            or original["revision"] != report["revision"]
            or original["registration"] != report["registration"]
            or original["requested_model"] != report["requested_model"]
            or original["fingerprints"]["wiki_net_cases.py"] != report["fingerprints"]["wiki_net_cases.py"]):
        raise RuntimeError("The completed source report does not match this pilot's frozen conditions")
    rows = original["results"]
    if len(rows) != len(plan):
        raise RuntimeError("The source report has an incomplete task list")
    retained, replacement_ids = [], []
    for item, row in zip(plan, rows):
        if any(row.get(key) != value for key, value in item.items()) or not all(arm in row for arm in ARMS):
            raise RuntimeError("Task order or arms differ in the source report")
        # The legacy parser overwrote the actual model with this marker on
        # provider errors. Recover only when both sides of that task are affected.
        if all(row[arm].get("model") == "<synthetic>" and row[arm].get("returncode") == 1
               for arm in ARMS):
            replacement_ids.append(row["id"])
        else:
            retained.append(row)
    if not replacement_ids or any(row[arm].get("model") == "<synthetic>"
                                  for row in retained for arm in ARMS):
        raise RuntimeError("No exclusively paired provider interruptions to recover")
    archive = source / "wiki-snapshot"
    snapshot = original["wiki_snapshot"]
    if (wiki_manifest(archive, repo, report["revision"]) != snapshot["pages"]
            or read_json(archive / "manifest.json") != snapshot):
        raise RuntimeError("The original Wiki snapshot differs from its recorded hashes")
    shutil.copytree(archive, output / "wiki-snapshot")
    for row in retained:
        if row["category"] == REPAIR:
            for arm in ARMS:
                name = f'{row["id"]}-{arm}.diff'
                if (source / name).is_file():
                    shutil.copyfile(source / name, output / name)
    report.update(
        results=retained, wiki_snapshot=snapshot, actual_model=original["actual_model"],
        recovery={"source_report": str(source / "report.json"),
                  "source_sha256": hashlib.sha256(raw).hexdigest(),
                  "retained_pairs": len(retained), "replacement_ids": replacement_ids},
    )
    print(f"Recovery retained {len(retained)} pairs and scheduled {len(replacement_ids)} pairs.",
          flush=True)


def run_pilot(args, output):
    report_path = output / "report.json"
    settings, provider_source = base.resolve_provider(base.MODEL_ENV)
    model = (args.model or settings.get("ANTHROPIC_MODEL") or settings.get("ANTHROPIC_DEFAULT_MODEL")
             or "haiku")
    client = SimpleNamespace(claude=args.claude, provider_env=settings, model=model,
                             timeout=REGISTRATION["timeout_seconds"])
    signature = fingerprints()
    if args.resume:
        report = read_json(report_path)
        if report.get("finished_at"):
            print("Pilot already completed. Use report to review the result.", flush=True)
            return 0
        if report["fingerprints"] != signature or report["requested_model"] != model:
            raise RuntimeError("The preregistered files or requested model changed")
    else:
        report = {"started_at": utcnow(), "revision": base.FIXTURES["revision"],
                  "requested_model": model, "provider_source": provider_source,
                  "fingerprints": signature, "registration": REGISTRATION,
                  "schedule": schedule(), "results": []}
    with tempfile.TemporaryDirectory(prefix="wiki-net-code-") as directory:
        repo = Path(directory) / "source"
        report["fixture_controls"] = prepare(args, repo)
        if args.recover_from:
            restore_interrupted_pilot(args.recover_from.resolve(), output, repo, report)
            report["summary"] = summarize(report)
            base.save(report_path, report)
        archived = output / "wiki-snapshot"
        if not shutil.which(args.claude):
            status = "claude_cli_missing"
        else:
            status = base.check_endpoint(settings)
        preflight = {"status": status} if status else base.claude(
            client, repo, "Respond with exactly EVAL_READY.", bare_tools=True)
        if preflight["status"] == "ok" and (preflight.get("answer", "").strip() != "EVAL_READY"
                                            or not preflight.get("model")):
            preflight["status"] = "unexpected_preflight_reply"
        report["latest_preflight"] = preflight
        report["summary"] = summarize(report)
        base.save(report_path, report)
        if preflight["status"] != "ok":
            print(f'Live preflight blocked: {preflight["status"]}', flush=True)
            return 2
        if report.get("actual_model", preflight["model"]) != preflight["model"]:
            raise RuntimeError("The provider returned a different model since the preceding run")
        report["actual_model"] = preflight["model"]
        if "wiki_snapshot" not in report:
            if archived.is_dir():
                report["wiki_snapshot"] = read_json(archived / "manifest.json")
            else:
                report["wiki_snapshot"] = freeze_wiki(
                    repo, repo.parent / "wiki-build", output, report["revision"])
            base.save(report_path, report)
        if wiki_manifest(archived, repo, report["revision"]) != report["wiki_snapshot"]["pages"]:
            raise RuntimeError("The frozen Wiki pages changed")
        cases = {case["id"]: case for case in CASES}
        completed = {row["id"]: row for row in report["results"]}
        for item in report["schedule"]:
            pair = completed.get(item["id"])
            if pair is None:
                pair = dict(item)
                report["results"].append(pair)
            for arm in pair["order"]:
                if arm not in pair:
                    pair[arm] = attempt(client, repo, archived, cases[pair["id"]],
                                        arm, output, report["actual_model"])
                    if pair[arm].get("provider_interruption"):
                        interruptions = report.setdefault("provider_interruptions", [])
                        for attempted_arm in ARMS:
                            if attempted_arm not in pair:
                                continue
                            patch = output / f'{pair["id"]}-{attempted_arm}.diff'
                            if patch.is_file():
                                archive_dir = output / "interrupted-patches" / str(len(interruptions) + 1)
                                archive_dir.mkdir(parents=True, exist_ok=True)
                                patch.replace(archive_dir / patch.name)
                        interruptions.append(pair)
                        report["results"].remove(pair)
                        report["summary"] = summarize(report)
                        base.save(report_path, report)
                        print(f'Provider interruption on {pair["id"]} {arm}: {pair[arm]["status"]}. '
                              "Use --resume after restoring the model connection.", flush=True)
                        return 2
                    report["summary"] = summarize(report)
                    base.save(report_path, report)
                    print(f'{pair["id"]} {arm} {pair[arm]["status"]} passed={pair[arm]["passed"]}',
                          flush=True)
        save_blind_reviews(report, output)
        report["finished_at"] = utcnow()
        base.save(report_path, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "run"):
        mode = sub.add_parser(name)
        mode.add_argument("--source", type=Path, default=base.ROOT)
        mode.add_argument("--bundle", type=Path, default=base.DEFAULT_BUNDLE)
    live = sub.choices["run"]
    live.add_argument("--output", required=True, type=Path)
    live.add_argument("--claude", default="claude")
    live.add_argument("--model")
    restart = live.add_mutually_exclusive_group()
    restart.add_argument("--resume", action="store_true")
    restart.add_argument("--recover-from", type=Path,
                         help="reuse the intact pairs and Wiki from a completed legacy pilot")
    view = sub.add_parser("report")
    view.add_argument("--output", required=True, type=Path)
    view.add_argument("--billing", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            with tempfile.TemporaryDirectory(prefix="wiki-net-fixtures-") as directory:
                controls = prepare(args, Path(directory) / "repo")
            print(f"Verified 30 answer keys and {len(controls)} clean, regression and restoration controls.")
            return 0
        output = args.output.resolve()
        if args.command == "report":
            report = read_json(output / "report.json")
            billing = load_billing(args.billing) if args.billing else None
            result = summarize(report, billing)
            if billing:
                base.save(output / "net-benefit.json", {"billing": billing, "summary": result})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        output.mkdir(parents=True, exist_ok=args.resume)
        with (output / ".run-lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return run_pilot(args, output)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Wiki pilot blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
