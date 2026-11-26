"""Extract source lookup metadata from Claude Code tool events."""

from pathlib import Path
import re

SEARCH_FILTERS = (
    "glob", "type", "-i", "-n", "-A", "-B", "-C", "context",
    "multiline", "head_limit", "offset",
)
TRACE_TEXT_LIMIT = 512
TRACE_LIST_LIMIT = 40


def trace_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value[:TRACE_TEXT_LIMIT]


def repo_path(value, root: Path, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or "\n" in value or "\0" in value:
        return None
    try:
        return trace_text((root / value).resolve().relative_to(root).as_posix(), secrets)
    except (OSError, RuntimeError, ValueError):
        return None


def source_request(tool: str, params: dict, root: Path, secrets: tuple[str, ...]) -> dict:
    if tool == "Read":
        fields = {"file": repo_path(params.get("file_path"), root, secrets)}
        fields.update({name: params[name] for name in ("offset", "limit")
                       if type(params.get(name)) is int})
        return fields

    fields = {
        "path": repo_path(params.get("path", "."), root, secrets),
        "output_mode": trace_text(params["output_mode"], secrets)
        if isinstance(params.get("output_mode"), str) else "files_with_matches",
    }
    pattern = params.get("pattern")
    if isinstance(pattern, str):
        fields["pattern"] = trace_text(pattern, secrets)
        if len(pattern) > TRACE_TEXT_LIMIT:
            fields["pattern_truncated"] = True
    for name in SEARCH_FILTERS:
        value = params.get(name)
        if isinstance(value, str):
            fields[name] = trace_text(value, secrets)
        elif type(value) in (int, bool):
            fields[name] = value
    return fields


def source_result(tool: str, data: dict, request: dict, root: Path, secrets: tuple[str, ...]) -> dict | None:
    if tool == "Read":
        file = data.get("file")
        if not isinstance(file, dict):
            return None
        path = repo_path(file.get("filePath"), root, secrets)
        start, count = file.get("startLine"), file.get("numLines")
        if path is not None and type(start) is int and type(count) is int and start > 0 and count > 0:
            return {"file": path, "line_start": start, "line_end": start + count - 1}
        return {"file": path} if path is not None else None

    mode = data.get("mode")
    if mode not in ("files_with_matches", "content", "count"):
        return None
    files = []
    if isinstance(data.get("filenames"), list):
        files.extend(path for name in data["filenames"] if (path := repo_path(name, root, secrets)))
    locations = []
    scope = request.get("path")
    single_file = scope if mode == "content" and isinstance(scope, str) and (root / scope).is_file() else None
    if single_file and type(data.get("numLines")) is int and data["numLines"] > 0:
        files.append(single_file)
    if mode in ("content", "count") and isinstance(data.get("content"), str):
        for line in data["content"].splitlines():
            name, colon, value = line.partition(":")
            number = value.partition(":")[0]
            if single_file:
                # For a file scope, the CLI drops the filename prefix.
                path, number = single_file, name
            else:
                path = repo_path(name, root, secrets) if colon else None
            if path is None:
                continue
            if not single_file:
                files.append(path)
            if (colon and mode == "content" and request.get("-n") is not False
                    and re.fullmatch(r"[0-9]{1,9}", number)):
                locations.append({"file": path, "line": int(number)})

    files = list(dict.fromkeys(files))
    summary = {"files": files[:TRACE_LIST_LIMIT]}
    total = data.get("totalFiles")
    if type(total) is int:
        summary["total_files"] = total
    if len(files) > TRACE_LIST_LIMIT or (type(total) is int and total > len(files)):
        summary["files_truncated"] = True
    if mode == "content" and type(data.get("numLines")) is int:
        summary["returned_lines"] = data["numLines"]
    if mode == "count" and type(data.get("numMatches")) is int:
        summary["total_matches"] = data["numMatches"]
    if locations:
        summary["locations"] = locations[:TRACE_LIST_LIMIT]
        if len(locations) > TRACE_LIST_LIMIT:
            summary["locations_truncated"] = True
    return summary
