# 第三周 · Wiki MCP、Skills 与来源核对

## 在 Claude Code 中连接仓库 Wiki

在 agent-core 根目录执行。已有 Wiki 页面会直接出现在查询结果中。

```bash
claude mcp add --scope project --transport stdio --env PYTHONPATH="$PWD" \
  -- repo-wiki "$(command -v python)" -m agent wiki-mcp "$PWD"
claude mcp list
```

项目级配置保存在 `.mcp.json`。首次使用时按 Claude Code 的提示批准该本地 Server。可用的三个工具均为只读。

| 工具 | 用途 |
| --- | --- |
| `list_pages()` | 列出仓库中已有的 Wiki 页面 |
| `search_wiki(query, limit)` | 用字面文本检索，返回页名、行号、字符偏移与片段 |
| `get_page(page, offset, limit)` | 读取页面 Markdown，按返回的 `next_offset` 续读 |

默认分页为 6,000 字符。服务端允许 Week 2 的四页 Wiki 和已写入的 `wiki/AGENTS.md`。当前连接使用 MCP stdio 和握手版本，stdio 的消息格式依据 [MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 按需启用 Skills

将每个 Skills 文件放在 `<目录>/<skill>/SKILL.md`。启用目录时，系统提示只附加 skill 名和说明。正文在模型调用 `read_skill` 时进入日志和上下文。

```markdown
---
name: wiki-research
description: 从仓库 Wiki 查找架构与来源。
---

先搜索 Wiki，再打开对应页面核对出处。
```

```bash
python -m agent run "从仓库 Wiki 查找审批流程" \
  --workspace . --skills-dir /path/to/skills --mcp-config /path/to/mcp.json --no-bash
```

Harness 的 MCP 配置需在 `allowedTools` 中明确列出可用工具，例如 `list_pages`、`search_wiki` 与 `get_page`。它沿用第一周的 MCP 配置格式。

续跑沿用原 run 记录的 Skills 目录及元数据。技能执行沿用现有审批和权限。限额为 50 个 Skills，每个正文最长 128 KiB，长结果沿用 `read_artifact` 的读取方式。

## 把外部核对记录写进依赖页

直接用 Playwright MCP 打开目标页并取快照。URL 需匹配所用配置的 `allowedDomains`。

```bash
python -m agent wiki . \
  --verify-url https://github.com/microsoft/playwright/issues/33566 \
  --browser-config examples/mcp.playwright.json
```

已在普通浏览器查阅的资料也可以通过 JSON 导入。

```bash
python -m agent wiki . --web-sources examples/wiki.web-sources.json
```

清单每项包含 `url`、`title`、带时区的 `fetched_at` 和 `excerpt`。页面保留这些证据供检查，抓取与导入是两条独立的输入路径。提供新证据时只触发外部依赖页的改动。续跑和常规更新沿用已有来源数据。

样例记录了在 Chrome 中核对的 Playwright 官方浏览器文档和维护者的 Chromium headless issue。文档说明了 Chromium headless shell 与 Chrome 新无头模式的差异。具体启动错误仍需根据本机浏览器日志判断。见 [Playwright Browsers](https://playwright.dev/docs/browsers) 和 [Issue #33566](https://github.com/microsoft/playwright/issues/33566)。
