# 第四周 · 仓库问答与修复评测

评测以 `6dda369` 为固定版本。30 道题覆盖日志与恢复、审批、MCP、Skills、Wiki、用量统计和模型配置。每道题记录 JSON 字段、标准答案和可追溯的源码依据。修复任务在同一版本的独立副本中构造。

## 正式评测结果

2026 年 11 月 26 日在 Mac 终端完成。运行客户端为 Claude Code，实际返回模型为 `deepseek-v4-pro`。30 道题均运行了两组，27 对满足配对判分和 token 统计条件。

| 指标 | 无 Wiki | 有 Wiki |
| --- | ---: | ---: |
| 完成会话 | 29/30 | 28/30 |
| 配对严格正确率 | 27/27（100%） | 25/27（92.59%） |
| 平均输入 token | 20,931.6 | 21,581.9 |
| 平均输出 token | 548.3 | 632.9 |
| 平均总 token | 21,479.9 | 22,214.9 |

有 Wiki 组在 13/27 对中实际调用了 Wiki 工具。本轮有 Wiki 组平均总 token 增加 3.42%。正式分数沿用预设的严格 JSON 评分。人工复核发现两个事实正确的格式失分，Q18 在 JSON 前输出了说明文字，Q22 将整数 `8` 输出成字符串 `"8"`。

Q11 和 Q15 的有 Wiki 会话以 `cli_error` 退出，Q30 的无 Wiki 会话以 `budget` 退出。这三题保留在运行记录中，配对分母为 27。

**修复任务完成率 4/4（100%）**。四个独立补丁通过各自的定向测试及全量回归。原始逐题结果和补丁记录位于 `runs/week4-live-retry/`。

## 运行评测

在仓库根目录使用 Claude Code 已可连接模型的终端运行。安装 Python 3.10+、Git 和 Claude Code。评测器优先使用当前进程的模型连接设置。缺少显式连接设置时，只继承 Claude Code 用户配置里的 API 地址、凭据和模型变量。默认使用已配置的模型，回退为 `haiku`。可用 `--model` 指定统一的模型。工作区里缺少固定提交时，程序会从仓库内的 `evals/fixtures/week3.bundle` 取得提交。各输出目录须为新目录。

```bash
python evals/benchmark.py --output runs/week4-fixtures --verify-fixtures
python evals/benchmark.py --output runs/week4-reproduction
```

可以用 `--limit 1 --jobs 1` 先试跑一对问答和四个修复任务。模型连接检查通过后才进入正式评测。每完成一对问答或一个修复任务便更新 `report.json`，最终包含模型、每题答案、工具调用和 token。可评分的补丁保存在同一目录。

## 对照与计分

- 两组读取同一个固定版本、使用同样的题目和只读源码工具。两组的模型会话各自独立。偶数题先运行有 Wiki 组。
- 有 Wiki 组通过独立的只读 stdio MCP Server 获取四张离线 Wiki 页面。代码工作目录内没有生成页面，标准答案在会话可访问的工作目录之外。
- 逐题比较 JSON 事实字段。问题要求的所有字段均正确才算该题正确。只有两组都完成、模型相同且 MCP 接入符合设定的一对问答才进入准确率分母。报告另计有 Wiki 组实际调用 Wiki 工具的次数。
- 输入 token 汇总普通输入、缓存写入和缓存命中，输出 token 单列。用量只比较双方都返回有效 usage 的配对。端点连接失败、超时和无法比较的会话均在报告中保留状态。
- 修复会话只开放读取和编辑工具。会话完成后，把允许修改的实现文件单独复制到干净的固定版本。定向测试及全量回归均通过才计为完成。

## 修复任务

| ID | 注入故障 | 判分依据 |
| --- | --- | --- |
| B01 | 分位数偏移取错样本 | Nearest-rank 单元测试 |
| B02 | Wiki 分页指针多前移一个字符 | 真实 stdio MCP 读完整页及边界测试 |
| B03 | HTTP 重试忽略数值 Retry-After | 注入 429 结果并核对等待调用 |
| B04 | 多行 Skills 描述混入 YAML 标记 | CLI 续跑前后及延迟披露测试 |

## 演示

视频展示了从中断续跑到带来源的 Wiki 查询，并摘录了本机实测命令和输出。[agent-core-demo.mp4](agent-core-demo.mp4)

[Claude Code CLI 命令参考](https://code.claude.com/docs/en/cli-reference) · [Claude Code 模型环境变量](https://code.claude.com/docs/en/env-vars)
