"""Frozen fresh questions for the Wiki A/B/C holdout."""

from wiki_net_cases import LOCATE, TRACE, IMPACT, question

QUESTIONS = [
    question(LOCATE, "同一 run 不能同时有两个写入者。返回负责取得独占文件锁的 file 和 function。",
             {"file": "agent/events.py", "function": "acquire_writer_lock"},
             ("agent/events.py", "def acquire_writer_lock(")),
    question(TRACE, "一次运行通过提交工具结束。返回提交工具处理函数 handler（函数名），"
                    "工具结果上触发停止的 outcome_flag（属性名），以及成功时写入停止事件的 stop_reason（字符串）。",
             {"handler": "tool_finish", "outcome_flag": "terminal", "stop_reason": "finish"},
             ("agent/tools.py", 'return _ok(args["answer"], terminal=True)'),
             ("agent/loop.py", '"reason": "finish" if outcome.ok else "tool_error"')),
    question(IMPACT, "计划把事件日志版本升为 2。按当前实现，返回 schema_file（定义版本常量的文件）、"
                     "json_key（事件中保存版本的字段）、current_version（整数）、"
                     "missing_uses_constant（旧日志缺少该字段时，读取是否回退到代码里的版本常量）。",
             {"schema_file": "agent/events.py", "json_key": "v", "current_version": 1,
              "missing_uses_constant": True},
             ("agent/events.py", "SCHEMA_VERSION = 1"),
             ("agent/events.py", '"v": self.v'),
             ("agent/events.py", 'd.get("v", SCHEMA_VERSION)')),
    question(LOCATE, "定位从 Playwright 页面快照文本提取 Page Title 或 Page URL 字段的通用辅助函数。返回 file 和 function。",
             {"file": "agent/wiki_sources.py", "function": "_page_field"},
             ("agent/wiki_sources.py", "def _page_field(")),
    question(TRACE, "两个并发的 MCP 请求先后发出，服务端把第二个响应先送回来。返回 client_file、"
                    "dispatcher（分发响应的方法名）、reply_key（响应关联请求的字段名）、"
                    "pending_map（存放请求到等待队列的属性名）。",
             {"client_file": "agent/mcp.py", "dispatcher": "_dispatch", "reply_key": "id",
              "pending_map": "_pending"},
             ("agent/mcp.py", "def _dispatch("),
             ("agent/mcp.py", 'rid = msg.get("id")'),
             ("agent/mcp.py", "box = self._pending.get(rid)")),
    question(IMPACT, "准备把单次工具耗时从毫秒迁移到秒。按当前实现，返回 run_file（写入耗时的文件）、"
                     "summary_file（读取日志并汇总的文件）、result_key（当前耗时字段）、"
                     "summary_property（单个工具累计耗时的属性）。",
             {"run_file": "agent/loop.py", "summary_file": "agent/usage.py",
              "result_key": "elapsed_ms", "summary_property": "total_ms"},
             ("agent/loop.py", '"elapsed_ms": elapsed_ms'),
             ("agent/usage.py", 'isinstance(data.get("elapsed_ms")'),
             ("agent/usage.py", "def total_ms(")),
    question(LOCATE, "定位可包裹任意模型 Provider，在发请求前检查工具消息协议的包装类。返回 file 和 wrapper（类名）。",
             {"file": "agent/llm.py", "wrapper": "ProtocolCheckingProvider"},
             ("agent/llm.py", "class ProtocolCheckingProvider:")),
    question(TRACE, "上次 Wiki 页面已经写入，新进程用同一生成内容重试写入。返回 writer_tool（注册的工具名）、"
                    "content_hash（算法名）、rewrites_identical（是否重新写入内容相同的页面）。",
             {"writer_tool": "write_wiki_page", "content_hash": "sha-256",
              "rewrites_identical": False},
             ("agent/wiki.py", 'ToolSpec("write_wiki_page"'),
             ("agent/wiki.py", "expected = hashlib.sha256(content).hexdigest()"),
             ("agent/wiki.py", "沿用上一轮落盘结果")),
    question(IMPACT, "准备修改 Skills front matter 的多行拼接规则。返回 parser_file、"
                     "parser（解析元数据文本的函数名）、reader（从文件读元数据并调用 parser 的函数名）、"
                     "discovery（扫描根目录并使用 reader 的函数名）。",
             {"parser_file": "agent/skills.py", "parser": "_metadata",
              "reader": "_header", "discovery": "discover_skills"},
             ("agent/skills.py", "def _metadata("),
             ("agent/skills.py", 'return _metadata(b"".join(chunks)'),
             ("agent/skills.py", "def discover_skills(")),
    question(LOCATE, "定位格式化 workspace 目录列表、给目录名加斜杠的内置工具处理函数。返回 file 和 function。",
             {"file": "agent/tools.py", "function": "tool_list_dir"},
             ("agent/tools.py", "def tool_list_dir(")),
    question(TRACE, "父运行目录有两个子运行。查询父运行的总模型调用次数时，返回 children_dir（父目录下存放子运行的目录名）、"
                    "collector（递归构造用量的函数名）、aggregate（跨父子合计属性的 RunUsage 方法名）、"
                    "includes_parent（合计是否也包含父运行自己的用量）。",
             {"children_dir": "subagents", "collector": "collect",
              "aggregate": "total", "includes_parent": True},
             ("agent/usage.py", 'sub_root = run_dir / "subagents"'),
             ("agent/usage.py", "u.children.append(collect("),
             ("agent/usage.py", "yield self"),
             ("agent/usage.py", "def total(")),
    question(IMPACT, "准备新增 Wiki MCP 支持的握手版本。返回 whitelist_file（支持版本集合所在文件）、"
                     "whitelist_name（集合名称）、default_file（共享默认版本常量定义文件）、"
                     "unknown_version_uses_default（未识别的请求版本是否回退到默认版本）。",
             {"whitelist_file": "agent/wiki_mcp.py", "whitelist_name": "PROTOCOLS",
              "default_file": "agent/mcp.py", "unknown_version_uses_default": True},
             ("agent/wiki_mcp.py", "PROTOCOLS ="),
             ("agent/wiki_mcp.py", "version if version in PROTOCOLS else PROTOCOL_VERSION"),
             ("agent/mcp.py", "PROTOCOL_VERSION =")),
]

CASES = [{**case, "id": f"H{index:02d}"} for index, case in enumerate(QUESTIONS, 1)]
