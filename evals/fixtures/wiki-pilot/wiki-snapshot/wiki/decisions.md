<!-- agent-core-wiki v1 {"dirty": [], "head": "6dda36907aa1825c90d090b14fdab0f69fe22879", "model": "deepseek-chat", "provider": "openai", "sources": {"README.md": "80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042"}} -->
# 设计决策

本页记录 agent-core 的核心设计取舍。所有内容均出自仓库根目录的 `README.md`（来源哈希由调用者添加）。README 只覆盖到「异步人工审批」一节的开头，因此本页不涉及该节之后的任何设计内容。

## 定位与约束

agent-core 是一个**零第三方依赖**（纯标准库，Python 3.10+）的可续跑 agent harness，面向**长任务自动化 / 研究**：崩溃能续、上下文有界、子任务可隔离扇出。设计上借鉴 DSH 架构的几条不变量——日志是唯一事实源、消息由日志推导、压缩只隐藏不删除——目标是通用长任务。（出处：`README.md`）

规模方面，README 给出 `wc -l agent/*.py` 的实测值：核心包共 4,519 行（含注释和文档字符串），其中 `loop.py` 550 行、`tools.py` 489 行、`mcp.py` 455 行、`wiki.py` 441 行。（出处：`README.md`）

## 五条不变量

README 明确表示「这五条是整个项目的全部价值。其余代码都只为它们服务」。逐条如下：（出处：`README.md`）

| # | 不变量 | 落点 |
|---|---|---|
| 1 | **append-only 事件日志是唯一事实源**：只追加、只 fsync、永不改写。唯一例外是崩溃留下的尾部半行会被截掉，并原样备份到 `events.jsonl.corrupt` | `events.py` |
| 2 | **模型可见的消息 = 日志的纯函数**：重启 / 续跑 / 复现逐字节一致 | `session.derive_messages` |
| 3 | **主循环没有内存态**：每步从日志推导下一步，杀在任意点都能接上 | `loop._next_action` |
| 4 | **安全边界写在代码结构里**（guard 单调），不写在提示词里 | `tools.execute` / `sandbox.py` |
| 5 | **压缩不破坏证据链**：只隐藏、不删除，摘要带 seq 指针 | `compact.py` |

这五条共同解释了项目为何把「日志」而非「内存状态」当作中心：可续跑（不变量 1、3）、可复现（不变量 2）、可审计（不变量 5）都从日志这一单一事实源派生；而安全约束被放进代码结构（不变量 4），避免依赖提示词这种非强制手段。

## 可验证性：demo 与测试

README 提供的入门路径本身即是对上述不变量的演示：`examples/demo.py` 不需要 API key，用确定性 mock 模型依次演示「正常跑完 → 进程被杀 → 续跑 → 等价性验证 → 长跑压缩有界」，正好对应不变量 1–3 与 5；测试用 `python -m unittest discover -s tests -v` 运行。（出处：`README.md`）

## 模型接入与配置优先级

真实模型通过项目根目录的 `.env` 配置 OpenAI 兼容端点、密钥与模型名（README 以 DeepSeek 为例，变量为 `HARNESS_BASE_URL`、`HARNESS_API_KEY`、`HARNESS_MODEL`）。`run`、`resume` 和 `wiki --provider openai` 在创建模型客户端时读取这三个设置。**优先级为：显式参数 > 终端环境变量 > 文件**。更换 API 地址时需为新端点提供对应密钥；`.env` 已被 Git 忽略，内置文件工具会跳过这份配置。（出处：`README.md`）

成本核算不写死单价，而是通过 `HARNESS_PRICE_IN`、`HARNESS_PRICE_OUT`、`HARNESS_PRICE_CACHE_HIT` 由使用者按模型与时段设置，`replay --usage` 可查看含子 agent 的成本与耗时。（出处：`README.md`）

## 可观测与恢复接口

README 把「看过程」与「看模型当时看到了什么」拆成不同命令，这与不变量 2 直接呼应：`replay <run_id> --events` 看事件流，`--messages` 看模型当时实际可见的消息，`--usage` 看成本与耗时；中断后用 `resume <run_id>` 续跑。（出处：`README.md`）

## 工具扩展：MCP 与产物落盘

MCP 工具接入以浏览器为例：`examples/browse_demo.py` 用脚本化的「模型」驱动真实浏览器，流程为打开文档页 → 取页面快照（约 100KB，被截断落盘）→ 用 `read_artifact` 在完整快照里搜出小节标题 → 再模拟「这一轮执行到一半进程被杀」后的续跑。该 demo 需要 Node（npx）与本机 Chrome，但不需要模型 API key；接真实模型时通过 `--mcp-config examples/mcp.playwright.json` 指定配置。（出处：`README.md`）

这里体现的设计取向是：大体积工具输出不直接塞进上下文，而是截断落盘、再按需检索，与「上下文有界」的目标一致。

## 范围说明

README 在「异步人工审批」一节仅给出 `run --async-approval`、`approvals list <run_id>` 等命令的开头，未展开其设计理由与实现细节。本页据此不对此部分作任何推断；如需补充，应以仓库中对应源码与后续文档为准。（出处：`README.md`，内容止于该节开头）

## 来源与 SHA-256

- `"README.md"` · `80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042`
