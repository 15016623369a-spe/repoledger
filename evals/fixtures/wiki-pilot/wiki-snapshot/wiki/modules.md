<!-- agent-core-wiki v1 {"dirty": [], "head": "6dda36907aa1825c90d090b14fdab0f69fe22879", "model": "deepseek-chat", "provider": "openai", "sources": {"agent/__init__.py": "9180eef6b9e794882dccf57a97d7bb0904f73f57633702c3d4df116f830a1225", "agent/__main__.py": "307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7", "agent/approval.py": "c2ffdbf9e59f66f7d26b7b6d4344885caa3d38821c92cc4f6d6ab96dec8dd597", "agent/cli.py": "517b9270967f358ee92ef15b4c73292fad043658b61f44d7de2f2c7b4b5c27be", "agent/compact.py": "487eabe7192602afca2be8b377a76ef9c4b8404918c1c3ef6a2d66ec646b32b4", "agent/context.py": "b5f58fd5104d504ce68d64d70b43f8d9f6c650145e026c2f67b4f1864a36d61d", "agent/events.py": "40740b3e7559a519594a5f4c5722210e36952bd3105f4db6817cfa06f967a7a0", "agent/llm.py": "4c05244ded2d87c68605732740a38657294979d7e57a3ded43b6f73d89552e42", "agent/loop.py": "73bfdc16eafaebfba26332e19f926d9de73be1593ca1c11eabceec6638c007ac", "agent/mcp.py": "5507c8b51b54b2e7d028571a0e2a7b19cc64340b42de218fbbe95c92e82adcc5", "agent/sandbox.py": "b4d73dedcbf7b312e6f6398b4ed6c93e2e630d830c6d1fbb0cef59814b18372f", "agent/session.py": "b4d2a354b0dd7ed3cf16aef333a82dd4e778cd96791234f730edd2d6b8c53f4a", "agent/settings.py": "ad8aacf7ccf38856262ab273a35a79d69499b3e6f1b3ad01c339d358a5ec8821", "agent/skills.py": "4bf2ab2b9086e13ae61fa45c3a575c36a70fee0d4ace357513ada0eb63f9d180", "agent/subagent.py": "907e30000d28ef99fa84655ef1a1f1cd798c4139dbf75b47edcfd4a2abebb266", "agent/tools.py": "940d1d3fc213025ca3b11a47e07e24eba9dbc60b3bd20598322b22eaac6647a3", "agent/usage.py": "3f3af5787908dbb6e90296a76065892d0bc1f362dfcb4258c00aab5b7ba3ce7d", "agent/wiki.py": "a96c6021baac5c1ca29822f981ed3d297d759ae930a616f4fa944bb96730084b", "agent/wiki_mcp.py": "c48597324a19a2f6f5d8cf2e406e5a455353038062dcfe312a4ece5cdd1f3714", "agent/wiki_sources.py": "f65250777f123e97a5082bb4a3231e1e7897c8cab03d6655e982687d3d8d5692", "examples/browse_demo.py": "e9fe4c2f4ec1678dea29f292be7629cebc60fe24ef56992a8523dbb52f871690", "examples/demo.py": "74a4c48b565de17f90d703aed2c85ff44662fbb08c06a00cef1626024550c2cb", "tests/fake_mcp_server.py": "3ae15f859c9b5473b926bfb4786b121d93aec91b36c82ffbacefe23e9665724f", "tests/test_approval.py": "0d4dd63048f7d4b109f4bee9a9d579ee7853e912f0f77bbc6aa158973268afa9", "tests/test_core.py": "77fb8f8986a81620fd8932361d54067927290beaa40c383ffaffb6371bbc0176", "tests/test_provider_config.py": "d03e041fb638af16c5a1d3705dd8782e8b13bc0ff008d7806bc616bfb7a38d5b", "tests/test_regressions.py": "c54bc870af564d6398ad57897928f9242f52ccec93cef08f04ce9f74dcc35a4a", "tests/test_week1.py": "6421790c9482115c807a4ee22fd5366e5e9821be338cdf34a36abcfb2d1892a5", "tests/test_week3.py": "8ee9974ec05734c68ced8504e49a0b8054e8e37a4cc39dc26be5d0c63f2e5973", "tests/test_wiki.py": "0c5e58a4beec1b1084568ad90d52f31af68cc20bb2b0f910157cb11cebb8908f"}} -->
# 模块索引

本页按仓库路径汇总 `agent/` 包对外暴露的模块与关键符号，供快速定位。所有条目均来自各文件顶部的模块说明与导出定义；未在片段中出现的模块（如 `settings`、`usage`、`wiki*`、`skills`、`subagent`、`tools`、`sandbox`、`session`）只列出被引用到的名字，不展开其内部实现。

## 包入口

### `agent/__init__.py`
包级导出。模块说明称对外只需 `Harness` / `HarnessConfig`（跑任务）、`Session`（读日志、做投影）、`derive_messages`（日志 → 消息，纯函数）。`__all__` 实际还包含：

- 循环：`Harness`、`HarnessConfig`、`RunResult`、`DEFAULT_SYSTEM`
- 会话：`Session`、`derive_messages`、`open_run`
- 事件：`EventType`、`Event`、`EventLog`
- 上下文：`Budget`、`estimate_tokens`
- 工具：`ToolRegistry`、`ToolSpec`、`ToolCall`、`default_registry`
- 沙箱/审批：`ApprovalGate`、`ApprovalRequest`、`check_command`、`resolve_within`
- 模型：`LLMError`、`LLMResponse`、`MockProvider`、`OpenAICompatProvider`
- 子 agent：`SUBAGENT_SYSTEM`

`__version__ = "0.1.0"`。

### `agent/__main__.py`
`python -m agent` 的入口：导入 `cli.main`，以 `SystemExit(main())` 退出。

## 核心模块

### `agent/events.py` — append-only 事件日志
系统唯一事实源。三条规则：只追加不修改不删除；模型可见内容必须能从日志重建；其它一切只是日志的只读投影。一行一事件，格式为 `{"v":1,"seq":N,"ts":"...","turn":K,"type":"...","data":{...}}`。

- `SCHEMA_VERSION = 1`
- `EventType`：事件类型登记表，拼错类型直接报错。登记的类型包括 `RUN_START`、`USER_INPUT`、`CONTEXT_SNAPSHOT`、`MODEL_REQUEST`、`MODEL_RESPONSE`、`TOOL_CALL`、`TOOL_RESULT`、`APPROVAL_REQUEST`、`APPROVAL_DECISION`、`GUARD_DENY`、`COMPACT_SUMMARY`、`SUBAGENT_SPAWN`、`SUBAGENT_RESULT`、`NOTICE`、`ERROR`、`RUN_STOP`，以及集合 `ALL`。
- `Event`（frozen dataclass）：`seq`、`ts`、`turn`、`type`、`data`、`v`；提供 `to_json()` / `from_dict()`。
- 异常：`EventLogCorrupted`（日志中间出现坏行，拒绝自动修复）、`EventLogBusy`（同一 run 已有写入进程）。
- `acquire_writer_lock(fd, path)`：跨平台（`msvcrt` / `fcntl`）非阻塞独占写锁。
- `EventLog`：追加写入，每次 append 后 flush + fsync。尾部坏行会物理截断到最后一个完整事件，被截字节存入 `events.jsonl.corrupt` 并记 notice；中间坏行按损坏处理（片段在此处截断，后续细节未给出）。

### `agent/context.py` — 上下文装配与预算
核心约束：请求 = 日志的纯函数，`assemble(session, budget)` 只看 events + config，同一日志两次请求逐字节一致。

- token 计量：不引入 tiktoken，采用可解释近似——CJK 1 字≈1 token，其它 4 字符≈1 token。`estimate_tokens(text)`、`estimate_messages_tokens(messages)`（每条消息加固定开销，tool_calls 的 arguments 单独计）。
- `Budget`：`max_turns=40`、`max_tool_calls=120`、`max_input_tokens=60_000`、`max_total_tokens=800_000`、`deadline_s=3600.0`；`from_config()`、`as_dict()`。
- `deadline_exceeded(session, budget)`：从 `run/start` 时间戳推算墙钟。
- `COLLAPSE_MARK`、`DEFAULT_TOOL_PREVIEW = 4000`。
- `Assembled`：`messages`、`est_tokens`、`collapsed`、`included_events`、`over_budget`；`header()` 生成写入日志的请求头（provider、model、tools、message_count、est_tokens_in、collapsed_tool_messages、included_events、over_budget）。
- `assemble(session, budget)`：确定性压力处理，从最旧的 tool 消息开始折叠为指针，直到估算 token 落到预算的 80%（片段在此截断）。

### `agent/compact.py` — 两档压缩
第一档剪枝在 `context.assemble` 中做（确定性折叠工具结果，不调模型）；第二档摘要在本模块做。三条铁律：只摘要"剪枝后仍超预算"的情况；已被摘要覆盖的轮次不再参与；摘要事件必须带 `covers` 指针，区间内旧摘要原文并入。

- 常量：`MAX_SUMMARY_TOKENS = 600`、`ITEM_CHARS = 180`、`DEFAULT_KEEP_RECENT = 3`。
- `CompactPolicy`：`trigger_ratio=0.70`、`keep_recent`；`should_compact()` 仅在 `assemble` 判定 `over_budget` 且超过阈值时返回真。
- 内部函数：`_turns`、`_turn_complete`（有模型响应且每个 tool/call 都有对应 result）、`_digest`（生成摘要素材并收集 artifact 指针，旧摘要按铁律 3 原文保留）、`_trim_to_tokens`（按 token 口径二分截断）。

### `agent/loop.py` — 主循环
设计要点：主循环没有内存态，每次迭代从日志推导下一步（`_next_action`），进程被杀后重开日志继续跑，行为与未崩溃一致。

- `DEFAULT_SYSTEM`：长任务 agent 的系统提示，含六条原则（先探明再动手、每次一件可验证小事、读错误修正参数、大任务用 `spawn_subagent`、长输出只摘片段并用 `read_artifact`、完成后调用 `finish`）。
- `new_run_id()`：`YYYYMMDD-HHMMSS-` + 4 位十六进制。
- `HarnessConfig`：`workspace`、`runs_root`、`system`、`budget`、`temperature`、`allow_bash`、`max_subagent_depth`、`preview_chars`、`compact_policy`、`max_parallel=4`、`extra`、`async_approval`；`to_dict(provider_name, depth)`。
- `RunResult`：`run_id`、`run_dir`、`reason`、`answer`、`stats`、`messages`；`ok` 在 `reason` 为 `finish` 或 `assistant_text` 时为真。
- `Harness`：构造参数含 `provider`、`config`、可选 `registry`、`approval`（片段在此截断）。

### `agent/llm.py` — 模型接入层
只用标准库（urllib），不引入 openai SDK。

- 异常：`LLMError`（loop 记 ERROR 事件并决定是否重试）；`SimulatedCrash`（测试用，继承 `BaseException`，不被兜底捕获）；`ProtocolError`（harness 自身拼错消息序列，同样继承 `BaseException`）。
- `LLMResponse`：`text`、`tool_calls`、`usage`；`as_event_data()` 转为事件数据。
- `Provider` 协议：`name` 属性 + `complete(messages, tools, *, temperature=0.0)`。
- `protocol_errors(messages)`：按 Chat Completions 规则检查——带 tool_calls 的 assistant 后必须紧跟其全部 tool 消息、不能有孤立 tool 消息、arguments 必须是 JSON 对象字符串。
- `ProtocolCheckingProvider`：包装任意 Provider，`strict=True` 时发现问题抛 `ProtocolError`，否则记入 `violations` 照常发送；其它属性透传（片段在此截断）。

### `agent/approval.py` — 异步审批投影
只有主线程或审批 CLI 写事件；审批状态从事件日志投影。

- `ApprovalPaused`：工具在副作用发生前暂停；子 run 暂停时请求保存在子日志。
- `Approvals`：从事件列表重建 `requests`（按 seq）、`decisions`（按 `request_seq`，`setdefault` 保留首个决策）、`stopped`（遇 `RUN_STOP`）；`pending()` 在已停止时返回空。
- `JournalApproval`：每个工具只读一份日志投影；`ask()` 按 `call_key` 或 `scope` 匹配已有决策，未获决策则抛 `ApprovalPaused(request)`。

### `agent/mcp.py` — MCP Client
纯标准库，通过子进程 stdio 通信。协议要点：每行一条 JSON-RPC 2.0、UTF-8；生命周期为 `initialize` → `notifications/initialized` → 之后才能 `tools/list`、`tools/call`；server 也可能主动发请求（如 ping）和通知（日志、进度）（片段在此截断）。

## 命令行

### `agent/cli.py`
子命令：`run` / `resume` / `replay` / `inspect`。模块说明给出示例：

```
python -m agent run "调研 labs/ 下的三个方案并给出选型" --workspace ./ws
python -m agent resume 20261124-153012-a1b2
python -m agent replay 20261124-153012-a1b2 --messages
python -m agent inspect 20261124-153012-a1b2
```

片段中可见的内部函数：

- `_provider(name, model)`：`mock` 返回脚本化 `MockProvider`（直接 `finish`，答案注明未接入真实模型），否则返回 `OpenAICompatProvider`。
- `_approval(args)`：`--yes` 全放行；非交互且无 `--yes` 时打印警告并拒绝所有需审批操作；交互时逐条询问 `[y/N]`。
- `_print_result(res, verbose)`：打印 run 信息、统计、答案、用量报告；`approval_pending` 时提示 `approvals list` 命令，`llm_error` 时提示 `resume`。
- `_tools(allow_bash, mcp_config, ws)`：内置工具 + 配置中的 MCP 工具，返回 `(registry, manager 或 None, 附加信息)`（片段在此截断）。

导入的模块包括 `approval`、`context`、`events`、`llm`、`loop`、`mcp`、`sandbox`、`session`、`settings`、`skills`、`tools`、`usage`、`wiki`、`wiki_mcp`、`wiki_sources`。

## 范围说明

- 片段在 `agent/events.py`、`agent/context.py`、`agent/compact.py`、`agent/llm.py`、`agent/loop.py`、`agent/mcp.py`、`agent/cli.py` 的末尾被截断，上述各节中标注"片段在此截断"处之后的实现细节未纳入本页。
- `settings`、`usage`、`wiki`、`wiki_mcp`、`wiki_sources`、`skills`、`subagent`、`tools`、`sandbox`、`session` 等模块仅以被导入或被引用的形式出现，本页不描述其内部结构。

## 来源与 SHA-256

- `"agent/__init__.py"` · `9180eef6b9e794882dccf57a97d7bb0904f73f57633702c3d4df116f830a1225`
- `"agent/__main__.py"` · `307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7`
- `"agent/approval.py"` · `c2ffdbf9e59f66f7d26b7b6d4344885caa3d38821c92cc4f6d6ab96dec8dd597`
- `"agent/cli.py"` · `517b9270967f358ee92ef15b4c73292fad043658b61f44d7de2f2c7b4b5c27be`
- `"agent/compact.py"` · `487eabe7192602afca2be8b377a76ef9c4b8404918c1c3ef6a2d66ec646b32b4`
- `"agent/context.py"` · `b5f58fd5104d504ce68d64d70b43f8d9f6c650145e026c2f67b4f1864a36d61d`
- `"agent/events.py"` · `40740b3e7559a519594a5f4c5722210e36952bd3105f4db6817cfa06f967a7a0`
- `"agent/llm.py"` · `4c05244ded2d87c68605732740a38657294979d7e57a3ded43b6f73d89552e42`
- `"agent/loop.py"` · `73bfdc16eafaebfba26332e19f926d9de73be1593ca1c11eabceec6638c007ac`
- `"agent/mcp.py"` · `5507c8b51b54b2e7d028571a0e2a7b19cc64340b42de218fbbe95c92e82adcc5`
- `"agent/sandbox.py"` · `b4d73dedcbf7b312e6f6398b4ed6c93e2e630d830c6d1fbb0cef59814b18372f`
- `"agent/session.py"` · `b4d2a354b0dd7ed3cf16aef333a82dd4e778cd96791234f730edd2d6b8c53f4a`
- `"agent/settings.py"` · `ad8aacf7ccf38856262ab273a35a79d69499b3e6f1b3ad01c339d358a5ec8821`
- `"agent/skills.py"` · `4bf2ab2b9086e13ae61fa45c3a575c36a70fee0d4ace357513ada0eb63f9d180`
- `"agent/subagent.py"` · `907e30000d28ef99fa84655ef1a1f1cd798c4139dbf75b47edcfd4a2abebb266`
- `"agent/tools.py"` · `940d1d3fc213025ca3b11a47e07e24eba9dbc60b3bd20598322b22eaac6647a3`
- `"agent/usage.py"` · `3f3af5787908dbb6e90296a76065892d0bc1f362dfcb4258c00aab5b7ba3ce7d`
- `"agent/wiki.py"` · `a96c6021baac5c1ca29822f981ed3d297d759ae930a616f4fa944bb96730084b`
- `"agent/wiki_mcp.py"` · `c48597324a19a2f6f5d8cf2e406e5a455353038062dcfe312a4ece5cdd1f3714`
- `"agent/wiki_sources.py"` · `f65250777f123e97a5082bb4a3231e1e7897c8cab03d6655e982687d3d8d5692`
- `"examples/browse_demo.py"` · `e9fe4c2f4ec1678dea29f292be7629cebc60fe24ef56992a8523dbb52f871690`
- `"examples/demo.py"` · `74a4c48b565de17f90d703aed2c85ff44662fbb08c06a00cef1626024550c2cb`
- `"tests/fake_mcp_server.py"` · `3ae15f859c9b5473b926bfb4786b121d93aec91b36c82ffbacefe23e9665724f`
- `"tests/test_approval.py"` · `0d4dd63048f7d4b109f4bee9a9d579ee7853e912f0f77bbc6aa158973268afa9`
- `"tests/test_core.py"` · `77fb8f8986a81620fd8932361d54067927290beaa40c383ffaffb6371bbc0176`
- `"tests/test_provider_config.py"` · `d03e041fb638af16c5a1d3705dd8782e8b13bc0ff008d7806bc616bfb7a38d5b`
- `"tests/test_regressions.py"` · `c54bc870af564d6398ad57897928f9242f52ccec93cef08f04ce9f74dcc35a4a`
- `"tests/test_week1.py"` · `6421790c9482115c807a4ee22fd5366e5e9821be338cdf34a36abcfb2d1892a5`
- `"tests/test_week3.py"` · `8ee9974ec05734c68ced8504e49a0b8054e8e37a4cc39dc26be5d0c63f2e5973`
- `"tests/test_wiki.py"` · `0c5e58a4beec1b1084568ad90d52f31af68cc20bb2b0f910157cb11cebb8908f`
