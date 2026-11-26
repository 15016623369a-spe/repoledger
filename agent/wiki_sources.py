"""为 Wiki 采集带时间与 URL 的外部证据。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from .mcp import MCPError, load_mcp_servers, tool_name
from .sandbox import ApprovalGate
from .tools import ToolCall, ToolContext, ToolRegistry

MAX_SOURCES = 8
MAX_EXCERPT = 2500


class SourceError(Exception):
    """外部来源未通过校验或采集失败。"""


class WebSource(TypedDict):
    url: str
    title: str
    fetched_at: str
    excerpt: str


def _host(url: str) -> str:
    if len(url) > 2048 or any(ch.isspace() or ch in "<>\\" for ch in url):
        raise SourceError("来源 URL 格式无效")
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return parsed.hostname.lower().rstrip(".")
    except ValueError:
        pass
    raise SourceError("来源 URL 必须是无凭据的 HTTP(S) 地址")


def _validate(rows: object) -> list[WebSource]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
        raise SourceError(f"来源清单必须包含 1 到 {MAX_SOURCES} 项")
    sources: list[WebSource] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(k), str) or not row[k].strip()
            for k in ("url", "title", "fetched_at", "excerpt")
        ):
            raise SourceError("来源需要 url、title、fetched_at 与 excerpt")
        url = row["url"].strip()
        _host(url)
        if url in seen:
            raise SourceError("来源清单包含重复 URL")
        seen.add(url)
        try:
            fetched = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceError("fetched_at 必须为含时区的 ISO 8601 时间") from exc
        if fetched.utcoffset() is None:
            raise SourceError("fetched_at 必须包含时区")
        sources.append({
            "url": url,
            "title": " ".join(row["title"].split())[:200],
            "fetched_at": fetched.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "excerpt": row["excerpt"].strip()[:MAX_EXCERPT],
        })
    return sources


def read_sources(path: Path) -> list[WebSource]:
    try:
        with path.open("rb") as f:
            data = f.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise SourceError("外部来源文件超过 128 KiB")
        return _validate(json.loads(data.decode("utf-8")))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SourceError(f"无法读取外部来源 JSON：{path}") from exc


def _page_field(text: str, field: str) -> str:
    match = re.search(rf"^[ \t]*-?[ \t]*Page {field}:[ \t]*(\S[^\n]*)", text, re.M)
    return match.group(1).strip() if match else ""


def capture_sources(repository: Path, config: Path, urls: list[str]) -> list[WebSource]:
    if not 1 <= len(urls) <= MAX_SOURCES or len(urls) != len(set(urls)):
        raise SourceError(f"请指定 1 到 {MAX_SOURCES} 个不同的来源 URL")
    for url in urls:
        _host(url)

    registry = ToolRegistry()
    nav, snap = (tool_name("playwright", key) for key in ("browser_navigate", "browser_snapshot"))
    ctx = ToolContext(repository, approval=ApprovalGate())
    rows = []
    try:
        with load_mcp_servers(config, registry, cwd=repository):
            if not registry.has(nav) or not registry.has(snap):
                raise SourceError("playwright MCP 配置需要启用 browser_navigate 和 browser_snapshot")
            for url in urls:
                navigated = registry.execute(ToolCall("navigate", nav, {"url": url}), ctx)
                if not navigated.ok:
                    raise SourceError(f"浏览器打开来源失败：{url}\n{navigated.content[:500]}")
                snapshot = registry.execute(ToolCall("snapshot", snap, {}), ctx)
                if not snapshot.ok:
                    raise SourceError(f"浏览器读取来源失败：{url}\n{snapshot.content[:500]}")
                final_url = _page_field(snapshot.content, "URL") or _page_field(navigated.content, "URL")
                if not final_url or _host(final_url) != _host(url):
                    raise SourceError(f"浏览器来源地址缺失或已跳转到其他主机：{url}")
                title = _page_field(snapshot.content, "Title") or _page_field(navigated.content, "Title")
                _, separator, fragment = snapshot.content.partition("### Snapshot")
                excerpt = fragment.strip() if separator else ""
                if not title or not excerpt:
                    raise SourceError(f"浏览器来源缺少页面标题或快照正文：{url}")
                rows.append({"url": final_url, "title": title, "excerpt": excerpt,
                             "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    except MCPError as exc:
        raise SourceError(f"浏览器 MCP 不可用：{exc}") from exc
    return _validate(rows)


def render_sources(sources: list[WebSource]) -> list[str]:
    if not sources:
        return []
    lines = ["", "## 外部核对来源", ""]
    for index, source in enumerate(sources, 1):
        title = source["title"].replace("<", "&lt;").replace(">", "&gt;")
        lines.extend([f"{index}. {title} · <{source['url']}> · 抓取于 {source['fetched_at']}", ""])
        lines.extend("> " + line.replace("<", "&lt;").replace(">", "&gt;")
                     for line in source["excerpt"].splitlines())
        lines.append("")
    return lines
