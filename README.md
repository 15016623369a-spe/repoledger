# RepoLedger

[![CI](https://github.com/15016623369a-spe/repoledger/actions/workflows/ci.yml/badge.svg)](https://github.com/15016623369a-spe/repoledger/actions/workflows/ci.yml)

**RepoLedger 是一个可续跑的仓库知识库 Agent。** 它从 Git 仓库生成带源码出处的 Wiki，按变更更新受影响的页面，并通过只读 MCP 提供给 Claude Code。运行时把模型调用、工具结果和人工审批写进追加式事件日志，中断后可以续跑，完成后可以回放。

核心仅使用 Python 3.10+ 标准库。生成离线 Wiki、跑演示和测试均无需 API key。

[演示视频](docs/agent-core-demo.mp4) · [运行时设计与使用边界](docs/runtime.md) · [Wiki MCP 与浏览器来源](docs/week3.md) · [策略与评测](docs/wiki-strategy.md)

## 快速体验

```bash
git clone https://github.com/15016623369a-spe/repoledger.git
cd repoledger
python3 examples/demo.py
python3 -m agent --runs examples/_demo/runs replay repo-impact --events
```

演示用确定性模型驱动真实工具，覆盖异常退出、续跑、上下文压缩及带源码行号的仓库分析。最后一条命令回放分析过程。

## 生成并查询仓库 Wiki

```bash
python3 -m agent wiki . --run-id wiki-v1
python3 -m agent approvals list wiki-v1
# 核对预览后，将 REQUEST_ID 换成上一步显示的编号
python3 -m agent approvals approve wiki-v1 REQUEST_ID
python3 -m agent resume wiki-v1
```

Wiki 默认生成到目标仓库的 `wiki/`，包含架构、模块、设计决策和依赖四页。每页记录依据的路径和 SHA-256。随后可用新的 run ID 再次生成，程序会结合 `git diff` 和来源哈希判断哪些页面需要重写。`wiki/AGENTS.md` 草稿写入前会暂停等待批准。

已安装 Claude Code 时，在仓库根目录注册只读服务。

```bash
claude mcp add --scope project --transport stdio --env PYTHONPATH="$PWD" \
  -- repo-wiki "$(command -v python3)" -m agent wiki-mcp "$PWD"
claude mcp list
```

在 Claude Code 中让 `repo-wiki` 列出页面、搜索主题或读取正文。依赖核对也可以接入 Playwright MCP，采集的网址和时间会保存在 Wiki。[MCP、Skills 和浏览器用法](docs/week3.md)

## 使用真实模型

在 RepoLedger 的项目根目录新建 `.env`，填入你自己的 OpenAI 兼容端点、API key 和模型名。

```dotenv
HARNESS_BASE_URL=https://api.deepseek.com/v1
HARNESS_API_KEY=your_api_key
HARNESS_MODEL=your_model_id
```

```bash
python3 -m agent run "工具耗时数据在哪里记录、在哪里汇总？给出源码行号" \
  --workspace . --no-bash --run-id repo-check
python3 -m agent replay repo-check --usage
```

运行中断后用 `python3 -m agent resume repo-check`。需要实验性的变更影响策略时，追加 `--strategy repo-impact-v2`。需要读 Wiki 时，可显式追加 `--mcp-config examples/mcp.repo-wiki.json`。需要人工确认的写入可使用 `--async-approval`，再通过 `approvals list`、`approve` 或 `deny` 作出决定。

## 真实评测

问答和修复评测在固定版本仓库上使用 Claude Code 与 DeepSeek。成功数按各轮预先定义的判分规则计算。

| 评测 | 结果 |
| --- | --- |
| 30 道仓库问答 | 27 对可比。源码组 27/27，Wiki 组 25/27。Wiki 组两道题事实正确、输出格式不符 |
| 4 个独立修复任务 | 4/4 通过定向测试与全量回归 |
| 40 道 Wiki 净收益试验 | 源码组 28/40，Wiki 组 30/40。配对差的 95% 区间为 −7.5 至 +17.5 个百分点 |
| 12 道新题对照 | 源码组 11/12，Wiki 组 9/12，Wiki + V2 组 11/12。V2 组没有调用 Wiki，较源码组多用 7.56% token |

当前数据不足以证明 Wiki 稳定提高正确率。仓库问答默认检索源码，Wiki 和 V2 由使用者选择开启。原生 CLI 的 V2 路径有离线集成回归，表中的模型成绩来自 Claude Code。[第四周报告](docs/week4.md) · [Wiki 净收益](docs/wiki-net-benefit.md) · [三组对照](docs/wiki-strategy.md)

## 检查与复现

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/wiki_holdout.py --verify
```

前两条覆盖运行时与评测脚本。第三条离线核对新题和固定版本的源码证据。重新运行真实模型评测会产生 API 用量，参数见各评测报告。

## 许可证

[MIT](LICENSE)
