"""Offline fixture validation and paired Claude Code evaluation for agent-core."""
from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from source_trace import source_request, source_result

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_BUNDLE = HERE / "fixtures/week3.bundle"
FIXTURES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
WIKI_TOOLS = ["mcp__repo_wiki__" + name for name in ("list_pages", "search_wiki", "get_page")]
INPUT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
BASE_TOOLS = ["Read", "Grep", "Glob"]
MODEL_ENV = {
    **os.environ, "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
CONNECTION_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
MODEL_KEYS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_OPUS_MODEL")
EXPLICIT_PROVIDER_KEYS = CONNECTION_KEYS + (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
TEST_ENV = {key: value for key, value in os.environ.items()
            if key in {"PATH", "HOME", "USER", "TMPDIR", "LANG", "TERM", "PYTHONUTF8"}
            or key.startswith("LC_")}


def resolve_provider(parent: dict[str, str]) -> tuple[dict[str, str], str]:
    """Preserve the selected endpoint and credentials as a group."""
    env = parent.copy()
    if any(env.get(key) for key in EXPLICIT_PROVIDER_KEYS):
        return env, "process_environment"

    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        payload = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return env, "claude_default"
    if not isinstance(payload, dict) or not isinstance(payload.get("env"), dict):
        return env, "claude_default"

    saved = payload["env"]
    if not any(isinstance(saved.get(key), str) and saved[key].strip() for key in CONNECTION_KEYS):
        return env, "claude_default"

    # --restricted skips the settings file. Carry only provider variables in
    # the subprocess environment so settings, hooks and tool policy stay isolated.
    for key in CONNECTION_KEYS + MODEL_KEYS:
        value = saved.get(key)
        if isinstance(value, str) and value.strip() and not env.get(key):
            env[key] = value
    return env, "claude_user_provider"


def run(command, cwd=None, *, timeout=60, env=None):
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def git(path, *arguments):
    proc = run(["git", "-C", str(path), *arguments], env=TEST_ENV)
    if proc.returncode:
        raise RuntimeError(f"git {arguments[0]} failed in {path.name}")
    return proc.stdout


def snapshot(source: Path, work: Path, bundle: Path | None) -> None:
    proc = run(["git", "clone", "-q", "--shared", "--no-checkout", str(source), str(work)],
               env=TEST_ENV)
    if proc.returncode:
        raise RuntimeError("could not copy the source repository")
    revision = FIXTURES["revision"]
    if run(["git", "-C", str(work), "cat-file", "-e", revision], env=TEST_ENV).returncode:
        if not bundle or not bundle.is_file():
            raise RuntimeError(f"revision {revision[:7]} is missing; pass a Week 3 bundle")
        git(work, "fetch", "-q", "--no-tags", str(bundle), "refs/heads/week3-wiki-mcp-skills")
    git(work, "checkout", "-q", "--detach", revision)
    if any((work / name).exists() for name in (".env", "wiki", "evals", ".mcp.json", "CLAUDE.md")):
        raise RuntimeError("benchmark source contains evaluation data, configuration, or the generated Wiki")


def validate_questions(repo: Path) -> None:
    questions = FIXTURES["qa"]
    if len(questions) != 30 or len({q["id"] for q in questions}) != len(questions):
        raise RuntimeError("expected exactly 30 distinct questions")
    for q in questions:
        if not q["fields"] or set(q["fields"]) != set(q["expected"]):
            raise RuntimeError(f"{q['id']}: invalid answer schema")
        for ref in q["evidence"]:
            if ref["needle"] not in (repo / ref["file"]).read_text(encoding="utf-8"):
                raise RuntimeError(f"{q['id']}: evidence does not match the pinned revision")


def scalar(value):
    if isinstance(value, list):
        return [scalar(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\s+", "", value.strip().strip("`")).casefold().replace("×", "*")
        return {
            "sha256": "sha-256",
            "字符": "characters", "字元": "characters", "character": "characters", "chars": "characters",
            "max(1,ceil(n*p))": "max(1,ceil(p*n))",
        }.get(value, value)
    return value


def answer_object(reply: str) -> dict | None:
    stripped = reply.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\n(.*?)\n```", stripped, re.I | re.S)
    try:
        submitted = json.loads(fenced.group(1) if fenced else stripped)
    except ValueError:
        return None
    return submitted if isinstance(submitted, dict) else None


def grade(question, reply: str) -> tuple[bool, list[str]]:
    submitted = answer_object(reply)
    if submitted is None:
        return False, list(question["expected"])
    missing_or_wrong = [
        key for key, expected in question["expected"].items()
        if key not in submitted or type(submitted[key]) is not type(expected)
        or scalar(submitted[key]) != scalar(expected)
    ]
    return not missing_or_wrong, missing_or_wrong


def qa_prompt(question) -> str:
    schema = json.dumps(question["fields"], ensure_ascii=False)
    return (
        "基于当前仓库的实现回答问题。可以使用本次会话提供的只读工具核对。"
        "只返回一个 JSON 对象，字段名使用给定的键，其余内容省略。"
        "未知值用 null，布尔值使用 JSON true 或 false。\n"
        f"问题：{question['question']}\n字段与格式：{schema}"
    )


def classify(text: str) -> str:
    text = text.casefold()
    for category, values in (
        ("billing", ("insufficient balance", "credit balance", "payment required", "insufficient_quota", "402")),
        ("network", ("enotfound", "econnrefused", "unable to connect", "network error", "eai_again",
                     "connection error", "connection reset")),
        ("auth", ("invalid api key", "authentication", "not logged in", "oauth token", "unauthorized", "401")),
        ("rate_limit", ("rate limit", "rate_limit", "429")),
        ("service_unavailable", ("overloaded", "service unavailable", "bad gateway", "503", "529")),
        ("budget", ("max_budget", "max_turns", "budget")),
    ):
        if any(re.search(r"\b" + value + r"\b", text) if value.isdigit() else value in text
               for value in values):
            return category
    return "cli_error"


def wiki_match_count(content) -> int | None:
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [block.get("text") for block in content if isinstance(block, dict)]
    else:
        return None
    for item in texts:
        if not isinstance(item, str):
            continue
        try:
            data = json.loads(item)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("matches"), list):
            return len(data["matches"])
    return None


def parse_stream(stdout: str, stderr: str, code: int, *, trace_tools=False,
                 trace_root: Path | None = None, trace_secrets: tuple[str, ...] = ()) -> dict:
    if trace_root is not None:
        trace_root = trace_root.resolve()
        trace_tools = True
    calls = Counter()
    result = {}
    model = None
    configured_model = None
    mcp_connected = False
    synthetic = []
    tool_trace = {}
    turns = {}
    for line_number, line in enumerate(stdout.splitlines()):
        try:
            packet = json.loads(line)
        except ValueError:
            continue
        if not isinstance(packet, dict):
            continue
        kind = packet.get("type")
        if kind == "system" and packet.get("subtype") == "init":
            configured_model = packet.get("model")
            mcp_connected = any(server.get("name") == "repo_wiki" and server.get("status") == "connected"
                                for server in packet.get("mcp_servers") or [] if isinstance(server, dict))
        if kind == "assistant":
            message = packet.get("message") or {}
            reported_model = message.get("model")
            if reported_model == "<synthetic>":
                synthetic.append(str(packet.get("error") or ""))
            elif reported_model:
                model = reported_model
            turn = None
            if trace_root is not None and reported_model != "<synthetic>":
                turn = turns.setdefault(message.get("id") or line_number, len(turns) + 1)
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                    tool = block["name"]
                    calls[tool] += 1
                    if trace_tools and block.get("id") and tool in BASE_TOOLS + WIKI_TOOLS:
                        entry = tool_trace.setdefault(block["id"], {"tool": tool, "outcome": "unobserved"})
                        if turn is not None:
                            entry["turn"] = turn
                            if tool in ("Grep", "Read") and isinstance(block.get("input"), dict):
                                entry["request"] = source_request(tool, block["input"],
                                                                  trace_root, trace_secrets)
                if (reported_model == "<synthetic>" and isinstance(block, dict)
                        and block.get("type") == "text"):
                    synthetic.append(str(block.get("text") or ""))
        if trace_tools and kind == "user":
            content = (packet.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            results = [block for block in blocks
                       if isinstance(block, dict) and block.get("type") == "tool_result"]
            native = packet.get("tool_use_result") if len(results) == 1 else None
            for block in results:
                entry = tool_trace.get(block.get("tool_use_id"))
                if entry is None:
                    continue
                entry["outcome"] = "error" if block.get("is_error") else "ok"
                if entry["tool"] == WIKI_TOOLS[1] and entry["outcome"] == "ok":
                    hits = wiki_match_count(block.get("content"))
                    if hits is not None:
                        entry["matches"] = hits
                if (trace_root is not None and entry["tool"] in ("Grep", "Read")
                        and entry["outcome"] == "ok" and isinstance(native, dict)):
                    details = source_result(entry["tool"], native, entry.get("request", {}),
                                            trace_root, trace_secrets)
                    if details is not None:
                        entry["result"] = details
        if kind == "result":
            result = packet

    usage = result.get("usage") or {}
    numbers = [usage.get(field, 0) for field in INPUT_KEYS]
    output = usage.get("output_tokens")
    valid_usage = (
        "input_tokens" in usage
        and all(type(n) is int and n >= 0 for n in numbers + [output])
        and sum(numbers) + output > 0
    )
    row = {"model": model, "configured_model": configured_model, "calls": dict(calls),
           "mcp_connected": mcp_connected,
           "input_tokens": sum(numbers) if valid_usage else None,
           "output_tokens": output if valid_usage else None,
           "usage": dict(zip((*INPUT_KEYS, "output_tokens"), (*numbers, output))) if valid_usage else None,
           "reported_cost_usd": result.get("total_cost_usd")}
    if trace_tools:
        row["tool_trace"] = list(tool_trace.values())
    subtype = result.get("subtype")
    if code or subtype != "success" or result.get("is_error") or (synthetic and not model):
        limit = subtype if subtype in {"error_max_turns", "error_max_budget_usd"} else None
        error = "\n".join([str(result.get("result", "")), str(result.get("errors", "")),
                           stderr, *synthetic])
        status = "budget" if limit else classify(error)
        interruption = not limit and (bool(synthetic) or status in {
            "billing", "network", "auth", "rate_limit", "service_unavailable",
        })
        return row | {
            "status": "synthetic_error" if synthetic and status == "cli_error" else status,
            "returncode": code,
            "limit": limit,
            "provider_interruption": interruption,
        }
    return row | {"status": "ok", "answer": result.get("result", "")}


def claude(args, repo: Path, prompt: str, *, wiki: Path | None = None,
           edit=False, bare_tools=False, system_append: str | None = None,
           trace_tools=False, trace_source=False) -> dict:
    tools = [] if bare_tools else BASE_TOOLS + (["Edit", "Write"] if edit else [])
    permitted = tools + (WIKI_TOOLS if wiki else [])
    config = {"mcpServers": {}}
    if wiki:
        config["mcpServers"]["repo_wiki"] = {
            "type": "stdio", "command": sys.executable,
            "args": ["-m", "agent", "wiki-mcp", str(wiki)],
        }
    command = [
        args.claude, "-p", "--restricted", "--output-format", "stream-json", "--verbose",
        "--model", args.model, "--effort", "low", "--no-session-persistence",
        "--disable-slash-commands", "--no-chrome", "--permission-mode", "dontAsk",
        "--permission-prompts", "none", "--strict-mcp-config",
        "--mcp-config", json.dumps(config), "--tools", ",".join(tools),
        "--max-turns", "12" if edit else "7", "--max-budget-usd", "0.50" if edit else "0.12",
    ]
    if permitted:
        command += ["--allowedTools", ",".join(permitted)]
    if system_append is not None:
        command += ["--append-system-prompt", system_append]
    trace_options = {"trace_tools": trace_tools}
    if trace_source:
        keys = (*CONNECTION_KEYS[1:], "HARNESS_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
        secrets = sorted({args.provider_env[key] for key in keys if args.provider_env.get(key)},
                         key=len, reverse=True)
        trace_options.update(trace_root=repo, trace_secrets=tuple(secrets))
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        p = run([*command, "--", prompt], repo, timeout=min(35, args.timeout) if bare_tools else args.timeout,
                env=args.provider_env)
    except subprocess.TimeoutExpired as exc:
        row = {"status": "timeout"}
        if (trace_tools or trace_source) and exc.stdout:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
            row["tool_trace"] = parse_stream(stdout, "", 1, **trace_options)["tool_trace"]
    else:
        row = parse_stream(p.stdout, p.stderr, p.returncode, **trace_options)
    row["seconds"] = round(time.monotonic() - started, 2)
    row["started_at"] = started_at
    row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


def check_endpoint(env: dict[str, str]) -> str | None:
    endpoint = urlsplit(env.get("ANTHROPIC_BASE_URL", ""))
    if endpoint.hostname in {"127.0.0.1", "::1", "localhost"}:
        try:
            port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
            with socket.create_connection((endpoint.hostname, port), timeout=3):
                pass
        except OSError:
            return "configured_loopback_proxy_unreachable"
    return None


def wiki_snapshot(source: Path, destination: Path, bundle: Path | None) -> None:
    snapshot(source, destination, bundle)
    runs = destination.parent / "wiki-runs"
    base = [sys.executable, "-m", "agent", "--runs", str(runs)]
    generated = run([*base, "wiki", ".", "--run-id", "eval-wiki"], destination, env=TEST_ENV)
    waiting = run([*base, "approvals", "list", "eval-wiki"], destination, env=TEST_ENV)
    pages = destination / "wiki"
    if (generated.returncode != 1 or waiting.returncode or not waiting.stdout[:1].isdigit()
            or len(list(pages.glob("*.md"))) != 4):
        raise RuntimeError("could not generate the four Wiki pages for the treatment arm")
    req = waiting.stdout.split(maxsplit=1)[0]
    if run([*base, "approvals", "deny", "eval-wiki", req], destination, env=TEST_ENV).returncode:
        raise RuntimeError("could not close the evaluation Wiki draft decision")
    if run([*base, "resume", "eval-wiki"], destination, env=TEST_ENV).returncode:
        raise RuntimeError("could not finish evaluation Wiki preparation")


def test_bug(repo: Path, case: dict) -> subprocess.CompletedProcess:
    packed = [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest"]
    env = {**TEST_ENV, "PYTHONPATH": "tests", "PYTHONDONTWRITEBYTECODE": "1"}
    return run([*packed, case["test"]], repo, env=env, timeout=25)


def seed_bug(repo: Path, case: dict) -> str:
    path = repo / case["file"]
    original = path.read_text(encoding="utf-8")
    if original.count(case["anchor"]) != 1:
        raise RuntimeError(f"{case['id']}: mutation anchor is not unique")
    if test_bug(repo, case).returncode:
        raise RuntimeError(f"{case['id']}: the pinned revision does not pass its targeted test")
    path.write_text(original.replace(case["anchor"], case["mutation"], 1), encoding="utf-8")
    test = test_bug(repo, case)
    if test.returncode != 1 or "Ran 1 test" not in test.stderr or "FAILED" not in test.stderr:
        raise RuntimeError(f"{case['id']}: the targeted test did not detect the seeded regression")
    return original


def bugfix(args, solution: Path, output: Path, case: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix=case["id"] + "-", dir=solution.parent) as tmp:
        repo = Path(tmp) / "repo"
        snapshot(solution, repo, None)
        seed_bug(repo, case)
        implementation = repo / case["file"]
        seeded = implementation.read_text(encoding="utf-8")
        row = claude(args, repo, case["question"] +
                     f"\n请修改当前仓库的 {case['file']}。只改该实现文件，保留现有行为。", edit=True)
        touched = [chunk[3:] for chunk in git(repo, "status", "--porcelain", "-z").split("\0") if chunk]
        row["id"] = case["id"]
        if row["status"] != "ok":
            return row | {"completed": None}
        if any(name != case["file"] for name in touched) or implementation.is_symlink() or not implementation.is_file():
            return row | {"completed": False, "reason": "invalid_change_scope", "files": touched}
        candidate = implementation.read_text(encoding="utf-8")
        if candidate == seeded:
            return row | {"completed": False, "reason": "no_edit"}
        patch = "".join(difflib.unified_diff(
            seeded.splitlines(keepends=True), candidate.splitlines(keepends=True),
            fromfile="a/" + case["file"], tofile="b/" + case["file"],
        ))
        (output / f"{case['id']}.diff").write_text(patch, encoding="utf-8")
        # Only transport the edited implementation into the clean grading checkout.
        grader = Path(tmp) / "grader"
        snapshot(solution, grader, None)
        (grader / case["file"]).write_text(candidate, encoding="utf-8")
        focused = test_bug(grader, case).returncode == 0
        suite = False
        if focused:
            suite = run([sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
                         "discover", "-s", "tests"], grader, timeout=60, env=TEST_ENV).returncode == 0
        return row | {"completed": focused and suite, "focused_pass": focused, "suite_pass": suite}


def compare_pair(args, repo: Path, wiki: Path, q: dict) -> dict:
    arms = ["without_wiki", "with_wiki"]
    if int(q["id"][1:]) % 2 == 0:
        arms.reverse()
    result = {"id": q["id"], "order": arms}
    for name in arms:
        row = claude(args, repo, qa_prompt(q), wiki=wiki if name == "with_wiki" else None)
        if row["status"] == "ok":
            row["correct"], row["wrong_fields"] = grade(q, row["answer"])
        result[name] = row
    return result


def summarize(pairs: list[dict], fixes: list[dict]) -> dict:
    paired = [pair for pair in pairs if all(pair[arm]["status"] == "ok"
              for arm in ("without_wiki", "with_wiki"))
              and pair["with_wiki"]["mcp_connected"] and pair["without_wiki"]["model"]
              and pair["without_wiki"]["model"] == pair["with_wiki"]["model"]
              and not any(name in WIKI_TOOLS for name in pair["without_wiki"]["calls"])]
    tokens = [pair for pair in paired if all(
        pair[arm]["input_tokens"] is not None for arm in ("without_wiki", "with_wiki"))]
    answers = {}
    for arm in ("without_wiki", "with_wiki"):
        values = [row[arm] for row in paired]
        comparable_tokens = [row[arm] for row in tokens]
        correct = sum(row["correct"] for row in values)
        answers[arm] = {
            "correct": correct, "denominator": len(values),
            "accuracy_pct": round(100 * correct / len(values), 2) if values else None,
            "mean_input_tokens": round(statistics.mean(row["input_tokens"] for row in comparable_tokens), 1)
                                if comparable_tokens else None,
            "mean_output_tokens": round(statistics.mean(row["output_tokens"] for row in comparable_tokens), 1)
                                 if comparable_tokens else None,
            "wiki_tool_used": sum(any(name in WIKI_TOOLS for name in row["calls"]) for row in values),
        }
    scorable = sum(job["completed"] is not None for job in fixes)
    completed = sum(job["completed"] is True for job in fixes)

    def difference(metric):
        old, new = (answers[arm][metric] for arm in ("without_wiki", "with_wiki"))
        return round(new - old, 2) if old is not None and new is not None else None

    return {
        "questions_attempted": len(pairs), "planned_questions": len(FIXTURES["qa"]),
        "comparable_pairs": len(paired), "comparable_token_pairs": len(tokens), "qa": answers,
        "wiki_minus_baseline": {
            "accuracy_points": difference("accuracy_pct"),
            "mean_input_tokens": difference("mean_input_tokens"),
            "mean_output_tokens": difference("mean_output_tokens"),
        },
        "bug_tasks_attempted": len(fixes), "planned_bug_tasks": len(FIXTURES["bugfix"]),
        "bug_tasks_scorable": scorable, "bug_tasks_completed": completed,
        "bug_completion_pct": round(100 * completed / scorable, 2) if scorable else None,
    }


def save(path: Path, data) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--output", type=Path, required=True, help="a new directory for the result")
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--model", help="override the configured model (fallback: haiku)")
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--limit", type=int, choices=range(1, 31), default=30,
                        metavar="1..30", help="evaluate the first N question pairs")
    parser.add_argument("--verify-fixtures", action="store_true", help="run only offline input and monitor checks")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    args.provider_env, provider_source = resolve_provider(MODEL_ENV)
    args.model = (args.model or args.provider_env.get("ANTHROPIC_MODEL")
                  or args.provider_env.get("ANTHROPIC_DEFAULT_MODEL") or "haiku")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source, bundle = args.source.resolve(), args.bundle.resolve()
    pairs, fixes = [], []
    report = {
        "revision": FIXTURES["revision"], "requested_model": args.model,
        "provider_source": provider_source,
        "cases_sha256": hashlib.sha256((HERE / "cases.json").read_bytes()).hexdigest(),
        "run_started_at": datetime.now(timezone.utc).isoformat(), "model_preflight": None,
        "fixtures": [], "qa_pairs": pairs, "bugfix": fixes,
    }
    with tempfile.TemporaryDirectory(prefix="agent-core-eval-") as directory:
        base = Path(directory)
        repo, wiki = base / "source", base / "wiki-source"
        snapshot(source, repo, bundle)
        validate_questions(repo)
        wiki_snapshot(repo, wiki, None)
        print("Verified 30 question evidence anchors and prepared the four-page Wiki.", flush=True)
        for case in FIXTURES["bugfix"]:
            original = seed_bug(repo, case)
            (repo / case["file"]).write_text(original, encoding="utf-8")
            if test_bug(repo, case).returncode:
                raise RuntimeError(f"{case['id']}: positive control did not recover")
            report["fixtures"].append({"id": case["id"], "selected_test": case["test"],
                                       "clean_pass": True, "seed_detected": True, "control_pass": True})
            print(f"{case['id']} fixture: clean passed, mutation detected, restored control passed.", flush=True)
        report["summary"] = summarize(pairs, fixes)
        save(output / "report.json", report)
        if args.verify_fixtures:
            return 0
        blocked = check_endpoint(args.provider_env)
        if blocked:
            report["model_preflight"] = {"status": blocked}
        elif not shutil.which(args.claude):
            report["model_preflight"] = {"status": "claude_cli_missing"}
        else:
            preflight = claude(args, repo, "Respond with exactly EVAL_READY.", bare_tools=True)
            if preflight["status"] == "ok" and preflight.get("answer", "").strip() != "EVAL_READY":
                preflight["status"] = "unexpected_preflight_reply"
            report["model_preflight"] = preflight
        save(output / "report.json", report)
        if report["model_preflight"]["status"] != "ok":
            print("Live evaluation blocked:", report["model_preflight"]["status"], flush=True)
            return 2

        print("Claude preflight completed; running paired questions and isolated repairs.", flush=True)
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            jobs = [pool.submit(compare_pair, args, repo, wiki, q) for q in FIXTURES["qa"][:args.limit]]
            for future in concurrent.futures.as_completed(jobs):
                row = future.result()
                pairs.append(row)
                report["summary"] = summarize(pairs, fixes)
                save(output / "report.json", report)
                print(f"{row['id']} {row['without_wiki']['status']} / {row['with_wiki']['status']}", flush=True)
        for case in FIXTURES["bugfix"]:
            row = bugfix(args, repo, output, case)
            fixes.append(row)
            report["summary"] = summarize(pairs, fixes)
            save(output / "report.json", report)
            print(f"{row['id']} {row['status']} completed={row['completed']}", flush=True)
    summary = report["summary"]
    complete = (summary["comparable_pairs"] == summary["comparable_token_pairs"] == args.limit
                and summary["bug_tasks_scorable"] == len(FIXTURES["bugfix"]))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
