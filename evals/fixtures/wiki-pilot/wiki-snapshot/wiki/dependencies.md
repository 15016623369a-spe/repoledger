<!-- agent-core-wiki v1 {"dirty": [], "head": "6dda36907aa1825c90d090b14fdab0f69fe22879", "model": "deepseek-chat", "provider": "openai", "sources": {".github/workflows/ci.yml": "5fc7d765b2897491be18a89ea0b95d6fbbef892596e8296af5fe764c9c01d2cb", "README.md": "80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042", "examples/mcp.playwright.json": "617c8793ff30733caf55f9374c915829dad8d2182f3eaf4eabaadabb2de0505d"}} -->
# 外部依赖

本页说明本仓库在运行、测试与可选功能上对外部组件的依赖情况。依据来源为 `.github/workflows/ci.yml`、`README.md` 与 `examples/mcp.playwright.json`。

## 核心运行时：零第三方依赖

README 明确声明本项目是**零第三方依赖**（纯标准库，Python 3.10+）的可续跑 agent harness。CI 配置中的注释也印证了这一点：`# 零第三方依赖：不需要 pip install，也不需要任何 API key`。

因此，运行核心流程与测试不需要安装任何第三方 Python 包。

来源：`README.md`、`.github/workflows/ci.yml`

## Python 版本要求

- README 声明最低版本为 **Python 3.10+**。
- CI 在 `ubuntu-latest` 上以矩阵方式测试 **3.10、3.12、3.13** 三个版本。

来源：`README.md`、`.github/workflows/ci.yml`

## 测试与离线演示

CI 中执行的两条命令均不涉及外部依赖安装：

- 单元与回归测试：`python -W error::ResourceWarning -m unittest discover -s tests -v`
- 离线演示（mock 模型，崩溃与续跑）：`python examples/demo.py`

README 的「跑起来」一节同样说明：不需要 API key，用确定性 mock 模型即可跑完整流程。

来源：`.github/workflows/ci.yml`、`README.md`

## 可选：真实模型端点

接入真实模型时，需要在项目根目录创建 `.env`，填入 OpenAI 兼容端点、密钥与模型名（README 以 DeepSeek 为例）：

```dotenv
HARNESS_BASE_URL=https://api.deepseek.com/v1
HARNESS_API_KEY=填写你的密钥
HARNESS_MODEL=deepseek-chat
```

`run`、`resume` 和 `wiki --provider openai` 会在创建模型客户端时读取这三个设置。优先级为：显式参数 > 终端环境变量 > 文件。更换 API 地址时需为新端点提供对应密钥。`.env` 已被 Git 忽略，内置文件工具会跳过该配置。

此外，费用统计可通过环境变量设置每百万 token 单价：`HARNESS_PRICE_IN`、`HARNESS_PRICE_OUT`、`HARNESS_PRICE_CACHE_HIT`。

来源：`README.md`

## 可选：MCP 工具（以浏览器为例）

接入 MCP 工具需要额外外部组件。以 Playwright 浏览器示例为例：

- 需要 **Node（npx）** 和**本机 Chrome**；不需要模型 API key。
- `examples/mcp.playwright.json` 中通过 `npx -y @playwright/mcp@0.0.85` 启动 MCP 服务，参数包含 `--headless`、`--isolated`、`--image-responses omit`。
- 该配置限定了允许的工具（`browser_navigate`、`browser_snapshot`、`browser_navigate_back`、`browser_wait_for`、`browser_close`）、允许的域名（`docs.python.org`、`fastapi.tiangolo.com`、`developer.mozilla.org`、`github.com`）以及 `timeoutSeconds: 90`。

来源：`README.md`、`examples/mcp.playwright.json`

## 未覆盖范围

来源片段未提供以下信息，故本页不作推断：

- 除 Playwright 之外的其他 MCP 服务端配置。
- 各外部依赖的具体版本锁定策略（除示例中固定的 `@playwright/mcp@0.0.85` 外）。
- 生产部署环境所需的系统级依赖清单。

## 来源与 SHA-256

- `".github/workflows/ci.yml"` · `5fc7d765b2897491be18a89ea0b95d6fbbef892596e8296af5fe764c9c01d2cb`
- `"README.md"` · `80b8a56e5cbbfbcc21db028c62f10835fac4e6047470151eb6c6c2f5b4c82042`
- `"examples/mcp.playwright.json"` · `617c8793ff30733caf55f9374c915829dad8d2182f3eaf4eabaadabb2de0505d`
