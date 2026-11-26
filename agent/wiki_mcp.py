"""只读 Wiki MCP Server。stdio 上每行一条 UTF-8 JSON-RPC 消息。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import BinaryIO

from .mcp import PROTOCOL_VERSION
from .settings import is_project_dotenv
from .wiki import HEADER, PAGES, WikiError, _target

PROTOCOLS = {"2024-11-05", "2025-03-26", PROTOCOL_VERSION, "2025-11-25"}
MAX_REQUEST = 64 * 1024
MAX_PAGE = 1024 * 1024
MAX_PAGE_CHARS = 20_000

TOOLS = [
    {"name": "list_pages", "description": "列出可查询的仓库 Wiki 页面。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "search_wiki", "description": "按文本检索仓库 Wiki，返回命中页、行号和匹配片段。",
     "inputSchema": {"type": "object", "required": ["query"], "additionalProperties": False,
                     "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "get_page", "description": "按文件名读取 Wiki 页面。offset 和 limit 按字符计，使用 next_offset 续读。",
     "inputSchema": {"type": "object", "required": ["page"], "additionalProperties": False,
                     "properties": {"page": {"type": "string", "enum": list(PAGES)},
                                    "offset": {"type": "integer", "minimum": 0},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_CHARS}}}},
]


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _integer(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise RPCError(-32602, f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


class WikiTools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _present(self) -> list[str]:
        pages = []
        for name in PAGES:
            try:
                path = _target(self.root, name)
            except (OSError, RuntimeError, WikiError):
                continue
            if path.is_file() and not is_project_dotenv(path):
                pages.append(name)
        return pages

    def _lines(self, name: str) -> list[str]:
        path = _target(self.root, name)
        if not path.is_file() or is_project_dotenv(path):
            raise WikiError(f"页面不存在或不可查询：{name}")
        with path.open("rb") as fh:
            data = fh.read(MAX_PAGE + 1)
        if len(data) > MAX_PAGE:
            raise WikiError(f"页面超过 {MAX_PAGE} 字节：{name}")
        lines = data.decode("utf-8").splitlines()
        # 哈希清单在 Markdown 正文已有一份，跳过不可见的内部元数据行。
        return lines[1:] if lines and lines[0].startswith(HEADER) else lines

    def list_pages(self) -> list[dict]:
        return [{"page": name, "title": PAGES[name]} for name in self._present()]

    def search(self, query: str, limit: int) -> list[dict]:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits = []
        for name in self._present():
            try:
                lines = self._lines(name)
            except (OSError, UnicodeError, WikiError):
                continue
            offset = 0
            for line_no, line in enumerate(lines, 1):
                match = pattern.search(line)
                if match:
                    start = max(0, match.start() - 60)
                    hits.append({"page": name, "line": line_no, "offset": offset + start,
                                 "excerpt": line[start:start + 220].strip()})
                    if len(hits) == limit:
                        return hits
                offset += len(line) + 1
        return hits

    def page(self, name: str, offset: int, limit: int) -> dict:
        markdown = "\n".join(self._lines(name))
        end = min(offset + limit, len(markdown))
        return {"page": name, "title": PAGES[name], "offset": offset, "total_chars": len(markdown),
                "next_offset": end if end < len(markdown) else None,
                "markdown": markdown[offset:end]}

    def call(self, name: object, args: object) -> dict:
        if not isinstance(args, dict):
            raise RPCError(-32602, "arguments 必须为对象")
        try:
            if name == "list_pages" and not args:
                data = {"pages": self.list_pages()}
            elif name == "search_wiki" and set(args) <= {"query", "limit"}:
                query = args.get("query")
                if not isinstance(query, str) or not 0 < len(query.strip()) <= 200:
                    raise RPCError(-32602, "query 必须为 1 到 200 字符的文本")
                data = {"matches": self.search(query.strip(), _integer(args, "limit", 10, 1, 20))}
            elif name == "get_page" and set(args) <= {"page", "offset", "limit"}:
                page = args.get("page")
                if not isinstance(page, str) or page not in PAGES:
                    raise RPCError(-32602, "请使用 list_pages 返回的 Wiki 文件名")
                data = self.page(page, _integer(args, "offset", 0, 0, MAX_PAGE),
                                 _integer(args, "limit", 6000, 1, MAX_PAGE_CHARS))
            else:
                raise RPCError(-32602, "未知工具或参数")
        except (OSError, UnicodeError, RuntimeError, WikiError):
            return {"isError": True,
                    "content": [{"type": "text", "text": "Wiki 页面不存在、无法读取或超出大小限制。"}]}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


class WikiMCPServer:
    def __init__(self, root: Path):
        self.tools = WikiTools(root)
        self.negotiated = False
        self.ready = False

    def _request(self, method: str, params: dict) -> dict:
        if method == "initialize":
            version = params.get("protocolVersion")
            if self.negotiated or not isinstance(version, str):
                raise RPCError(-32602, "initialize 需要协议版本且只允许一次")
            self.negotiated = True
            return {"protocolVersion": version if version in PROTOCOLS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-core-wiki", "version": "0.3"}}
        if method == "ping":
            return {}
        if not self.ready:
            raise RPCError(-32002, "请先完成 MCP 初始化")
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.tools.call(params.get("name"), params.get("arguments", {}))
        raise RPCError(-32601, "未知方法")

    def handle(self, raw: bytes) -> dict | None:
        response = {"jsonrpc": "2.0", "id": None}
        try:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise RPCError(-32700, "无效的 JSON") from None
            if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
                    or not isinstance(msg.get("method"), str)):
                raise RPCError(-32600, "无效的 JSON-RPC 请求")
            method = msg["method"]
            if "id" not in msg:
                if method == "notifications/initialized" and self.negotiated:
                    self.ready = True
                return None
            if type(msg["id"]) not in (int, str):
                raise RPCError(-32600, "请求 ID 必须为整数或字符串")
            response["id"] = msg["id"]
            params = msg.get("params", {})
            if not isinstance(params, dict):
                raise RPCError(-32602, "params 必须为对象")
            response["result"] = self._request(method, params)
        except RPCError as exc:
            response["error"] = {"code": exc.code, "message": str(exc)}
        return response

    def serve(self, incoming: BinaryIO, outgoing: BinaryIO) -> None:
        while line := incoming.readline(MAX_REQUEST + 1):
            if len(line) > MAX_REQUEST:
                while line and not line.endswith(b"\n"):
                    line = incoming.readline(MAX_REQUEST + 1)
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "请求超过大小限制"}}
            else:
                response = self.handle(line)
            if response is not None:
                outgoing.write((json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8"))
                outgoing.flush()
