"""Pair Wiki alone with Wiki plus an evidence policy on the eight discordant tasks."""

from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import statistics
import tempfile
from types import SimpleNamespace

import benchmark as base
import wiki_net as pilot

ARMS = ("with_wiki", "wiki_with_strategy")
STRATEGY = """仓库证据决策策略
- 根据任务定位难度选择第一步。题目给出了明确文件、符号、事件或可直接搜索的具体行为时，
  先用 Grep 或 Read 定位并读到相关源码上下文。遇到相似常量或函数时，核对实际调用位置。
- 要追踪跨模块行为或判断修改影响，而且入口未知时，从一个具体行为或符号发起 Wiki 检索。
  用命中片段缩小待查范围。Wiki 目录只能导航，行为与测试结论以当前源码为准。
- Wiki 首次无命中、与任务不相关或与代码冲突时，只有在已有更明确检索词的情况下再查一次。
  搜索达到两次，或已有可验证的源码位置时，立即切到源码核实，避免反复列目录或翻整页。
- 确认所有答案字段后结束。证据不足的字段按任务约定处理。
  发送前核对字段名和 JSON 类型，最终答复保持原题规定的 JSON 结构。"""
STRATEGIES = {
    "v1": STRATEGY,
    "v2": """仓库证据决策策略 V2
- 先用题目中的文件、符号或行为限定源码搜索范围。同一模块的线索尽量合并到一次 Grep，带上相关行的上下文。
  如果命中片段已支持答案字段，就采用该证据。只有上下文不够时才 Read 命中附近的片段。
- 源码入口不清楚时，用具体线索查 Wiki 来定位。只有已经找到更具体的关键词才再查一次，
  然后转向源码证实需要回答的行为。
- 每次调用工具前，判断它能补齐哪个尚缺证据的字段。字段都已有依据时立即输出原题要求的 JSON。
  证据仍有缺口时按原题处理未知值，保留规定的字段名与类型。""",
}


def default_baseline() -> Path:
    local = base.ROOT / "runs/wiki-net-recovery"
    return local if (local / "report.json").is_file() else base.HERE / "fixtures/wiki-pilot"


def baseline_conditions(baseline: Path, repo: Path) -> tuple[dict, dict]:
    report_path = baseline / "report.json"
    report = pilot.read_json(report_path)
    revision = base.FIXTURES["revision"]
    if (report["revision"] != revision or not report.get("finished_at")
            or report["fingerprints"]["wiki_net_cases.py"] != pilot.fingerprints()["wiki_net_cases.py"]
            or report["summary"]["pairs_completed"] != len(pilot.CASES)):
        raise RuntimeError("A completed pilot with the same tasks and fixed revision is required")
    pages = pilot.wiki_manifest(baseline / "wiki-snapshot", repo, revision)
    if report["wiki_snapshot"]["pages"] != pages:
        raise RuntimeError("The baseline Wiki hashes changed")
    return report, {
        "baseline_report": str(report_path), "baseline_sha256": pilot.digest(report_path),
        "wiki_pages": pages, "revision": revision, "requested_model": report["requested_model"],
        "actual_model": report["actual_model"],
        "files": pilot.fingerprints() | {
            name: pilot.digest(base.HERE / name) for name in ("wiki_strategy.py", "source_trace.py")
        },
    }


def experiment(baseline: Path, repo: Path, strategy_version: str = "v1", *,
               tasks=(), trace_source=False) -> tuple[dict, dict]:
    report, conditions = baseline_conditions(baseline, repo)
    cases = {case["id"]: case for case in pilot.CASES}
    selection = sorted((row for row in report["results"]
                        if row["without_wiki"]["passed"] != row["with_wiki"]["passed"]),
                       key=lambda row: row["id"])
    ids = [row["id"] for row in selection]
    if ids != ["W02", "W11", "W12", "W17", "W18", "W21", "W23", "W25"]:
        raise RuntimeError("The baseline does not contain the eight reviewed discordant tasks")
    unknown = set(tasks) - set(ids)
    if unknown:
        raise RuntimeError(f"Tasks are outside the eight reviewed cases: {', '.join(sorted(unknown))}")
    chosen = set(tasks or ids)
    for task in chosen:
        for evidence in cases[task]["evidence"]:
            if evidence["needle"] not in (repo / evidence["file"]).read_text(encoding="utf-8"):
                raise RuntimeError(f"{task}: source evidence changed")
    return conditions | {
        "schedule": [{"id": task, "order": list(ARMS if index % 2 == 0 else ARMS[::-1])}
                     for index, task in enumerate(ids) if task in chosen],
        "historical_outcomes": {row["id"]: {arm: row[arm]["passed"] for arm in pilot.ARMS}
                                for row in selection if row["id"] in chosen},
        "strategy_version": strategy_version, "strategy": STRATEGIES[strategy_version],
        "trace_source": trace_source,
    }, cases


def summarize_arms(rows, arms):
    pairs = [row for row in rows if all(arm in row for arm in arms)]
    usage_pairs = [row for row in pairs if all(row[arm].get("usage") for arm in arms)]
    metrics = {}
    for arm in arms:
        results = [row[arm] for row in pairs]
        metrics[arm] = {
            "passed": sum(row["passed"] for row in results),
            "budget_stops": sum(row["status"] == "budget" for row in results),
            "timeouts": sum(row["status"] == "timeout" for row in results),
            "wiki_calls": sum(sum(row.get("calls", {}).get(tool, 0) for tool in base.WIKI_TOOLS)
                              for row in results),
            "source_calls": sum(sum(row.get("calls", {}).get(tool, 0) for tool in base.BASE_TOOLS)
                                for row in results),
            "median_seconds": statistics.median(row["seconds"] for row in results) if results else None,
            "matched_usage_tokens": sum(row[arm]["input_tokens"] + row[arm]["output_tokens"]
                                        for row in usage_pairs),
        }
    return pairs, len(usage_pairs), metrics


def summarize(rows):
    pairs, usage_count, arms = summarize_arms(rows, ARMS)
    return {
        "pairs_completed": len(pairs), "paired_usage_count": usage_count, "arms": arms,
        "only_with_strategy": [row["id"] for row in pairs
                               if not row[ARMS[0]]["passed"] and row[ARMS[1]]["passed"]],
        "only_plain_wiki": [row["id"] for row in pairs
                           if row[ARMS[0]]["passed"] and not row[ARMS[1]]["passed"]],
    }


def run_comparison(args, output, repo, manifest, cases, *, summarize_results=summarize):
    target = output / "report.json"
    design = manifest.get("design", "diagnostic_on_previously_seen_tasks")
    if args.resume:
        report = pilot.read_json(target)
        previous = report["experiment"]
        if previous.get("strategy_version", "v1") != manifest["strategy_version"]:
            raise RuntimeError("The strategy version differs from the existing comparison")
        if report.get("design", "diagnostic_on_previously_seen_tasks") != design:
            raise RuntimeError("The experiment design differs from the existing comparison")
        if (previous.get("schedule") != manifest.get("schedule")
                or previous.get("trace_source", False) != manifest.get("trace_source", False)):
            raise RuntimeError("The task selection or source tracing differs from the existing comparison")
        if report.get("finished_at"):
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            return 0
        if report["experiment"] != manifest:
            raise RuntimeError("The experiment inputs changed since the start of this comparison")
    else:
        report = {"design": design, "experiment": manifest,
                  "started_at": pilot.utcnow(), "results": [], "interruptions": []}

    archive = repo.parent / "wiki-archive"
    shutil.copytree(args.baseline / "wiki-snapshot" / "wiki", archive / "wiki")
    if pilot.wiki_manifest(archive, repo, manifest["revision"]) != manifest["wiki_pages"]:
        raise RuntimeError("The copied Wiki differs from the frozen snapshot")

    env, provider = base.resolve_provider(base.MODEL_ENV)
    client = SimpleNamespace(claude=args.claude, provider_env=env, model=manifest["requested_model"],
                             timeout=pilot.REGISTRATION["timeout_seconds"])
    status = "claude_cli_missing" if not shutil.which(args.claude) else base.check_endpoint(env)
    probe = {"status": status} if status else base.claude(
        client, repo, "Respond with exactly EVAL_READY.", bare_tools=True)
    if probe["status"] == "ok" and (probe.get("answer", "").strip() != "EVAL_READY"
                                   or probe.get("model") != manifest["actual_model"]):
        probe["status"] = "unexpected_preflight_model_or_reply"
    report.update(latest_preflight=probe, provider_source=provider,
                  summary=summarize_results(report["results"]))
    base.save(target, report)
    if probe["status"] != "ok":
        print(f'Live preflight blocked: {probe["status"]}', flush=True)
        return 2

    attempted = {row["id"]: row for row in report["results"]}
    for task in manifest["schedule"]:
        pair = attempted.get(task["id"])
        if pair is None:
            pair = dict(task)
            report["results"].append(pair)
        for arm in task["order"]:
            if arm in pair:
                continue
            wiki_arm = "without_wiki" if arm == "without_wiki" else "with_wiki"
            row = pilot.attempt(client, repo, archive, cases[task["id"]], wiki_arm, output,
                                manifest["actual_model"], trace_tools=True,
                                trace_source=manifest.get("trace_source", False),
                                system_append=manifest["strategy"] if arm == "wiki_with_strategy" else None)
            if (wiki_arm == "with_wiki" and row.get("mcp_connected") is False
                    and not row.get("provider_interruption")):
                row |= {"status": "mcp_configuration_mismatch", "passed": False}
            pair[arm] = row
            error = row.get("provider_interruption") or row["status"] in {
                "model_mismatch", "mcp_configuration_mismatch",
            }
            if error:
                report["interruptions"].append(pair)
                report["results"].remove(pair)
            report["summary"] = summarize_results(report["results"])
            base.save(target, report)
            print(f'{task["id"]} {arm} {row["status"]} passed={row["passed"]}', flush=True)
            if error:
                print("Comparison paused. Resume to rerun both sides of this task.", flush=True)
                return 2

    report["finished_at"] = pilot.utcnow()
    base.save(target, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "run"))
    parser.add_argument("--baseline", type=Path, default=default_baseline())
    parser.add_argument("--source", type=Path, default=base.ROOT)
    parser.add_argument("--bundle", type=Path, default=base.DEFAULT_BUNDLE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strategy", choices=STRATEGIES, default="v1")
    parser.add_argument("--task", action="append", default=[], help="Select a review task, repeat for a subset")
    parser.add_argument("--trace-source", action="store_true", help="Record Grep queries and Read line ranges")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--claude", default="claude")
    args = parser.parse_args()
    if args.command == "run" and args.output is None:
        parser.error("run requires --output")
    args.baseline = args.baseline.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="wiki-strategy-") as tmp:
            repo = Path(tmp) / "source"
            base.snapshot(args.source.resolve(), repo, args.bundle.resolve())
            manifest, cases = experiment(args.baseline, repo, args.strategy,
                                         tasks=args.task, trace_source=args.trace_source)
            if args.command == "verify":
                ids = ", ".join(x["id"] for x in manifest["schedule"])
                print(f"Verified {args.strategy}, fixed source, Wiki and selected review cases: {ids}")
                return 0
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=args.resume)
            with (output / ".run-lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return run_comparison(args, output, repo, manifest, cases)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Wiki strategy comparison blocked: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
