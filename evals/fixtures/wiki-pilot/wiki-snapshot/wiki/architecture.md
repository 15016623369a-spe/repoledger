<!-- agent-core-wiki v1 {"dirty": [], "head": "6dda36907aa1825c90d090b14fdab0f69fe22879", "model": "deepseek-chat", "provider": "openai", "sources": {"README.md": "80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042", "agent/__main__.py": "307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7", "agent/cli.py": "517b9270967f358ee92ef15b4c73292fad043658b61f44d7de2f2c7b4b5c27be"}} -->
# 项目架构

本页依据仓库根目录 `README.md` 与 `agent/__main__.py`、`agent/cli.py` 的片段整理。片段未覆盖的模块内部实现不在本页断言范围内。

## 定位与依赖

`agent-core` 是一个**零第三方依赖**的可续跑 agent harness，纯标准库，要求 Python 3.10+。面向长任务自动化与研究：崩溃可续、上下文有界、子任务可隔离扇出。设计借鉴 DSH 架构的若干不变量，目标是通用长任务。

规模数据来自 `wc -l agent/*.py` 实测：核心包共 4,519 行（含注释与文档字符串），其中 `loop.py` 550 行、`tools.py` 489 行、`mcp.py` 455 行、`wiki.py` 441 行。

## 五条不变量

README 将以下五条列为项目的全部价值，其余代码都为其服务：

| # | 不变量 | 落点 |
|---|---|---|
| 1 | append-only 事件日志是唯一事实源，只追加、只 fsync、永不改写（唯一例外：崩溃留下的尾部半行被截掉，原样备份到 `events.jsonl.corrupt`） | `events.py` |
| 2 | 模型可见的消息 = 日志的纯函数，重启/续跑/复现逐字节一致 | `session.derive_messages` |
| 3 | 主循环没有内存态，每步从日志推导下一步，杀在任意点都能接上 | `loop._next_action` |
| 4 | 安全边界写在代码结构里（guard 单调），不写在提示词里 | `tools.execute` / `sandbox.py` |
| 5 | 压缩不破坏证据链：只隐藏、不删除，摘要带 seq 指针 | `compact.py` |

## 入口与 CLI 分层

`agent/__main__.py` 仅做一件事：从 `.cli` 导入 `main`，在 `__main__` 下以 `main()` 的返回值作为退出码。因此 `python -m agent ...` 的实际分发全部在 `agent/cli.py`。

`cli.py` 的模块文档字符串声明了四个子命令：`run` / `resume` / `replay` / `inspect`，并给出示例调用形式（`run` 带 `--workspace`、`resume`/`replay`/`inspect` 带 run_id，`replay` 可加 `--messages`）。

从 `cli.py` 的导入清单可以读出 CLI 层依赖的模块边界：

- `approval`（`Approvals`）：人工审批记录
- `context`（`Budget`）：上下文预算
- `events`（`EventLogBusy`、`EventType`）：事件日志
- `llm`（`MockProvider`、`OpenAICompatProvider`、`ProtocolCheckingProvider`）：模型提供方
- `loop`（`DEFAULT_SYSTEM`、`Harness`、`HarnessConfig`、`new_run_id`）：主循环与运行标识
- `mcp`（`MCPError`、`load_mcp_servers`）：MCP 工具接入
- `sandbox`（`ApprovalGate`、`ApprovalRequest`）：审批闸门
- `session`（`Session`）：会话
- `settings`（`ProviderConfigError`）：配置读取
- `skills`（`SkillsError`、`discover_skills`、`register_skills`、`skill_system`）：技能发现与注册
- `tools`（`default_registry`）：内置工具注册表
- `usage`（`Pricing`、`collect`、`format_report`）：用量与成本
- `wiki`（`WikiError`、`WikiPlan`、`WikiProvider`、`plan_wiki`、`wiki_registry`）：Wiki 生成
- `wiki_mcp`（`WikiMCPServer`）：Wiki 的 MCP 服务端
- `wiki_sources`（`SourceError`、`capture_sources`、`read_sources`）：Wiki 素材采集

## CLI 内部的关键构造

片段中可见的辅助函数体现了架构上的几处取舍：

- **`_provider(name, model)`**：`mock` 走 `MockProvider`，其策略固定返回一次 `finish` 工具调用，答案为「mock provider：未接入真实模型」；其余情况走 `OpenAICompatProvider(model=model)`。即真实模型统一按 OpenAI 兼容协议接入。
- **`_approval(args)`**：审批闸门有三种形态——`--yes` 时 `ApprovalGate.allow_all()`；非交互环境（`stdin` 非 tty）且未加 `--yes` 时返回默认 `ApprovalGate()` 并在 stderr 警告「所有需审批操作将被拒绝」；交互环境下用 `input` 询问 `[y/N]`，仅 `y`/`yes` 放行。
- **`_tools(allow_bash, mcp_config, ws)`**：先由 `default_registry(allow_bash=...)` 建内置工具注册表；若给了 MCP 配置，则解析为绝对路径后调用 `load_mcp_servers(path, registry, cwd=ws)`，把 MCP 工具注册进同一注册表，并打印 manager 的 warnings 与各 server 的摘要。函数注释说明返回三元组 `(registry, manager 或 None, 写进日志的附加信息)`。
- **`_print_result(res, verbose)`**：输出 run_id、run 目录、结束原因与成功标志、统计（轮数、工具调用、错误、guard 拒绝、压缩次数、子 agent 数）、token 计数、最终答案，以及 `format_report(collect(res.run_dir), Pricing.from_env())` 的成本报告。当 `reason == "approval_pending"` 时提示用 `approvals list` 查看待办；当 `reason == "llm_error"` 时提示处理后 `resume`。

## 运行与续跑（README）

无需 API key 即可用确定性 mock 模型跑完整流程：`python examples/demo.py` 依次演示正常跑完 → 进程被杀 → 续跑 → 等价性验证 → 长跑压缩有界。测试用 `python -m unittest discover -s tests -v`。

接真实模型时在项目根目录建 `.env`，填 `HARNESS_BASE_URL`、`HARNESS_API_KEY`、`HARNESS_MODEL`（README 以 DeepSeek 为例）。`run`、`resume` 与 `wiki --provider openai` 在创建模型客户端时读取这三项；优先级为显式参数 > 终端环境变量 > 文件。`.env` 已被 Git 忽略，内置文件工具会跳过这份配置。

中断后的恢复路径：`resume <run_id>` 接着跑；`replay <run_id> --events` 看过程；`--messages` 看模型当时看到的消息；`--usage` 看成本与耗时（含子 agent）。成本单价通过 `HARNESS_PRICE_IN` / `HARNESS_PRICE_OUT` / `HARNESS_PRICE_CACHE_HIT` 环境变量设置，不写死在代码里。

## 扩展面

- **MCP 工具**：`examples/browse_demo.py` 用脚本化「模型」驱动真实浏览器（需 Node/npx 与本机 Chrome，不需模型 API key），演示打开文档页 → 取页面快照（约 100KB，被截断落盘）→ 用 `read_artifact` 在完整快照里搜小节标题 → 模拟「执行到一半进程被杀」后的续跑。接真实模型时用 `--mcp-config examples/mcp.playwright.json`。
- **异步人工审批**：`run ... --async-approval` 启动，`approvals list <run_id>` 或 `approvals` 查看待办。
- **Wiki**：`wiki` 子命令与 `wiki --provider openai` 在 README 中被提及，`cli.py` 导入 `wiki`、`wiki_mcp`、`wiki_sources` 三个模块；其内部流程片段未覆盖，此处不作推断。

## 证据范围说明

本页所有结构性结论均来自上述两个文件的片段。`loop.py`、`events.py`、`session.py`、`compact.py`、`sandbox.py`、`tools.py` 等模块的内部实现细节（除 README 表格中标注的函数名外）未在片段中出现，故不展开。

## 来源与 SHA-256

- `"README.md"` · `80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042`
- `"agent/__main__.py"` · `307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7`
- `"agent/cli.py"` · `517b9270967f358ee92ef15b4c73292fad043658b61f44d7de2f2c7b4b5c27be`
