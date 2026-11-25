# agent-core

[![CI](https://github.com/15016623369a-spe/simple-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/15016623369a-spe/simple-agent/actions/workflows/ci.yml)

一个**零第三方依赖**（纯标准库，Python 3.10+）的可续跑 agent harness。

面向 **长任务自动化 / 研究**：崩溃能续、上下文有界、子任务可隔离扇出。
设计上借鉴了 DSH 架构的几条不变量（日志是唯一事实源、消息由日志推导、压缩只隐藏不删除），目标是通用长任务。

**规模**：核心包 2,382 行（`wc -l agent/*.py`，含注释和文档字符串），最大的是 `tools.py` 436 行、`loop.py` 435 行。
骨架六个模块（events / session / loop / tools / context / compact）共 1,671 行，其余是沙箱、子 agent、CLI 和模型接入。

---

## 五条不变量

这五条是整个项目的全部价值。其余代码都只为它们服务。

| # | 不变量 | 落在哪 |
|---|---|---|
| 1 | **append-only 事件日志是唯一事实源**，只追加、只 fsync、永不改写（唯一例外：崩溃留下的尾部半行会被截掉，原样备份到 `events.jsonl.corrupt`） | `events.py` |
| 2 | **模型可见的消息 = 日志的纯函数**，重启/续跑/复现逐字节一致 | `session.derive_messages` |
| 3 | **主循环没有内存态**，每步从日志推导下一步，杀在任意点都能接上 | `loop._next_action` |
| 4 | **安全边界写在代码结构里**（guard 单调），不写在提示词里 | `tools.execute` / `sandbox.py` |
| 5 | **压缩不破坏证据链**：只隐藏、不删除，摘要带 seq 指针 | `compact.py` |

---

## 跑起来

```bash
git clone https://github.com/15016623369a-spe/simple-agent.git
cd simple-agent

# 不需要 API key：用确定性 mock 模型跑完整流程
python examples/demo.py

# 跑测试
python -m unittest discover -s tests -v
```

`examples/demo.py` 会依次演示：正常跑完 → 进程被杀 → 续跑 → 等价性验证 → 长跑压缩有界。

### 接真实模型

任何 OpenAI 兼容端点都能用（OpenAI / DeepSeek / Moonshot / vLLM / Ollama / OpenRouter）：

```bash
export HARNESS_BASE_URL=https://api.deepseek.com/v1
export HARNESS_API_KEY=sk-...
export HARNESS_MODEL=deepseek-chat

python -m agent run "调研 src/ 的模块依赖，找出循环引用并写一份报告" \
  --workspace ./ws --max-turns 40 --max-depth 1
```

中断了就：

```bash
python -m agent resume <run_id>          # 接着跑
python -m agent replay <run_id> --events # 看过程
python -m agent replay <run_id> --messages  # 看模型当时到底看到了什么
```

---

## 文件地图

```
agent/
  events.py    EventType 登记 + append-only 写入 + fsync + 尾部半行物理截断并备份（中间坏行直接报错）
  session.py   Session（append / derive_messages / stats）+ 日志→消息的纯函数投影
  context.py   token 计量（CJK 友好）+ Budget + assemble（请求装配与确定性折叠）
  compact.py   第二档压缩：滚动合并摘要，带 covers/originals/artifacts 指针
  tools.py     validate → guards(单调) → dispatch → prune → finalize
  sandbox.py   路径边界（含 symlink 逃逸）+ 危险命令负向清单 + 审批门（fail closed）
  llm.py       Provider 协议 + OpenAI 兼容（urllib）+ Mock/Scripted + 崩溃注入 + 消息协议校验
  loop.py      turn 状态机 + 预算 + 子 agent 扇出 + 续跑
  subagent.py  SUBAGENT_SYSTEM + 子预算 + 子运行目录
  cli.py       run / resume / replay
tests/test_core.py   25 个测试，覆盖全部五条不变量（每次请求都先过消息协议校验）
tests/test_regressions.py  31 个回归测试：续跑消息协议、参数编码、非法 JSON 参数、审批默认拒绝、
                           日志截断、模型报错可续跑、子 agent 续跑、bash 环境变量与搜索边界、
                           摘要位置、edit_file 重放、read_file 超大与二进制文件
examples/demo.py     端到端演示
```

---

## 三个核心机制

### 1. 为什么它崩了能续

循环每一步都不知道自己"进行到哪了"——它每次重新推导：

```python
def _next_action(self, session) -> str:
    if session.log.of_type(RUN_STOP):        return "done"
    if self._pending_calls(session):         return "exec_tools"   # 崩溃在工具执行中间
    resp = self._last_response(session)
    if resp and not resp.data["tool_calls"]: return "finalize_text"
    return "call_model"                                            # 崩溃在模型调用中间
```

- 崩在**模型调用中** → 日志尾部是 `model/request` 无响应 → 重新发请求。
- 崩在**工具执行中** → 有 `tool/call` 无 `tool/result` → 重新执行该工具（工具应幂等）。
- **模型接口调不通**（重试用完）→ 只记 `run/error`，不写 `run/stop`，run 暂停在 `model/request`，排查后 `resume` 接着请求。重试会遵守 429 / 503 返回的 `Retry-After`。
- 崩在**子 agent 里** → 子 agent 的 id 由「哪条模型响应里的哪个调用」决定，父 run 续跑时重新执行同一个调用，会接上原来的子日志继续跑；子 agent 已经跑完就直接取回结论。
- 崩溃时**只写了半行日志** → 加载时把半行截掉并备份，之后的写入从干净的行尾开始。

因为判定只看日志，所以"续跑"和"没崩过"走的是同一段代码路径。测试里 `test_resume_equals_clean_run` 直接断言两者最终答案与工具序列完全一致。

续跑会往日志里追加「从日志恢复执行」这类提示。API 要求带 `tool_calls` 的 assistant 消息后面紧跟全部 tool 结果，所以 `derive_messages` 会把落在两者之间的提示延后到这一轮结果补齐之后再投影。`test_regressions.py` 用一个按 OpenAI 消息规则校验请求的假模型守着这一点。

### 2. 工具流水线的失败分层

```python
outcome = registry.execute(call, ctx)
```

| 失败类型 | 处理 | 是否关轮次 |
|---|---|---|
| 参数校验失败（含非法 JSON 参数） | 普通错误结果回灌，模型自己修 | ❌ |
| **guard 拒绝** | **单调生效，第一个 deny 就是终局** | ✅ |
| 工具执行异常 | 普通错误结果回灌，可换路重试 | ❌ |
| 沙箱越界 | 关轮次 | ✅ |

关轮次时，同一轮里后续没跑的调用会**补一条合成 `tool/result`**——否则下一次请求的 tool_call 数量对不上，API 直接报错。

守卫单调性的测试：

```python
def g1(call, ctx): return GuardDecision(True, "g1", "永远拒绝")
def g2(call, ctx): return None           # 想放行
# → 断言 g2 根本不会被调用
assert order == ["g1"]
```

### 3. 压缩：先剪枝，再摘要，且摘要要滚动合并

两档，代价递增：

1. **剪枝**（`context.assemble`，不调模型）：把最旧的 tool 结果折叠成指针，保留最近 2 条。
2. **摘要**（`compact.py`，**只在剪无可剪时**）：把旧轮次 + 上一条摘要合并成**一条**滚动摘要。

做错的两个坑（第一版都踩了，有测试守着）：

- ❌ **按原始大小触发** → 每轮都压，压缩风暴。✅ 改成 `assemble(...).over_budget` 才触发。
- ❌ **摘要层层追加** → 4 条摘要并存，比原文还占地方。✅ 改成滚动合并，`covers` 始终从 seq=1 起，旧摘要原文并入新摘要，**同时只有一条摘要生效**。

实测 40 轮连续任务：

```
轮次   压缩前est   摘要token   可见消息
   5       1121       259        10
  10       1615       638        10
  20       1751       654        10
  30       1750       648        10
  40       1623       521        10      ← 收敛，不随轮次增长
```

被折叠的内容虽然模型看不见了，但 `originals` 里的 seq 仍可从 `events.jsonl` 原样读回——**证据链没断**。

摘要事件是在压缩发生时才写进日志的，但它讲的是更早的历史，所以投影时放在它覆盖区间的起点（紧跟 system），后面才是保留下来的最近几轮，时间线不会倒过来。

---

## 怎么扩展

### 加一个工具

```python
from agent.tools import ToolSpec, Outcome, ToolError

def tool_count_words(ctx, args):
    p = ctx.root() / args["path"]          # 用 ctx.root() 保证在沙箱内
    if not p.exists():
        raise ToolError("文件不存在")       # 普通错误 → 回灌给模型
    return Outcome(ok=True, content=f"{len(p.read_text().split())} words")

registry.register(ToolSpec(
    name="count_words",
    description="统计文件词数",
    parameters={"type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"]},
    handler=tool_count_words,
))
```

大结果会自动落 `artifacts/` 并在上下文里留指针，不用自己裁剪。

### 加一个守卫（安全策略）

```python
from agent.tools import GuardDecision

def no_writes_outside_src(call, ctx):
    if call.name == "write_file" and not call.args.get("path", "").startswith("src/"):
        return GuardDecision(True, "scope", "只允许写 src/ 下的文件")
    return None

registry.guard(no_writes_outside_src)
```

注意 guard 是**单调**的：一旦拒绝，后面注册的 guard 无法翻案。

### 接自己的模型后端

实现三个成员即可：

```python
class MyProvider:
    name = "my-llm"
    def complete(self, messages, tools, *, temperature=0.0) -> LLMResponse:
        ...  # 返回 LLMResponse(text=..., tool_calls=[...], usage={...})
```

调试时可以包一层协议校验：`ProtocolCheckingProvider(MyProvider())`。每次请求前检查「带 tool_calls 的 assistant 后面紧跟全部 tool 结果、arguments 是 JSON 对象」，默认发现问题直接抛 `ProtocolError`；`strict=False` 只记到 `violations`，照常发请求。测试里所有 harness 都套着它。

---

## 故意不做的

| 不做 | 为什么 |
|---|---|
| 插件树 / 依赖注入容器 | 工具白名单 6–8 个，`ToolRegistry` 足够 |
| 多表面 / profile / bundle | 单入口 CLI，`--provider` 一个参数搞定 |
| 事件格式迁移链 | 起步 `{"v":1}`，真到 v3 再补 |
| 投影缓存 / FTS 索引 | 日志按需读，评测时内存折叠 |
| 多 agent 协作 | 只做**单向扇出**：子 agent 独立上下文，只回一条结论 |

子 agent 的隔离是刻意设计的三件事：独立 `events.jsonl`、独立预算（父的 1/3 轮次、1/4 token）、只回传 O(1) 的结论。所以父上下文不会被 40 轮子任务污染。子 agent 目录名形如 `subagents/sub-<响应 seq>-<调用 id>`，续跑靠它找回同一个子日志。

---

## 测试覆盖

```
Ran 56 tests in 0.072s
OK
```

重点几个：

| 测试 | 验证什么 |
|---|---|
| `test_resume_equals_clean_run` | 崩溃续跑与完整跑的工具序列、最终答案完全一致 |
| `test_crash_between_tool_call_and_result` | 崩在工具执行中间，续跑重新执行该工具 |
| `test_resume_is_idempotent_when_finished` | 已完成的 run 再 resume 新增 0 事件 |
| `test_guard_denial_is_monotonic` | 第一个 deny 后不再调用后续 guard |
| `test_denial_closes_turn_and_backfills_results` | 关轮次时补合成结果，保持协议合法 |
| `test_validation_error_does_not_close_turn` | 参数错不关轮次 |
| `test_rejects_symlink_escape` | symlink 逃逸被拦 |
| `test_dangerous_commands_denied` | `rm -rf /`、`curl\|sh`、fork bomb 等被拒 |
| `test_compaction_keeps_system_and_pointers` | 摘要不覆盖 system，保留 originals + artifacts 指针 |
| `test_truncates_corrupt_tail` | 崩溃留下的半行被截断且留 notice，之后仍能继续写 |
| `test_crash_between_two_tool_calls` | 崩在两个工具调用之间，续跑后的请求仍符合消息协议 |
| `test_arguments_are_replayed_verbatim` | 回放给模型的工具参数就是模型原样产出的 JSON |
| `test_resume_is_not_stuck_on_invalid_json` | 模型给出非法 JSON 参数时错误回灌，续跑不会卡死 |
| `test_default_gate_denies_write_edit_and_bash` | 没配置审批回调时，写文件、编辑文件、bash 全部被拒 |
| `test_tail_half_line_is_truncated_on_disk` | 截断后写入的事件，下次加载时仍然都在 |
| `test_llm_error_pauses_run_and_resume_finishes` | 模型接口失败只暂停 run，resume 能接着跑完 |
| `test_parent_resume_continues_child_log` | 崩在子 agent 里，续跑接上原子日志，已做的工具不重做 |
| `test_bash_cannot_echo_api_key` | bash 里读不到 `HARNESS_API_KEY` 等密钥 |
| `test_summary_is_projected_right_after_system` | 压缩后唯一生效的摘要紧跟 system，后续轮次按时间排列 |
| `test_replaying_edit_whose_new_contains_old` | `foo → foo bar` 重放一次不会变成 `foo bar bar` |
| `test_oversized_file_result_is_still_pruned` | 超过 2MB 的文件读出来照样裁剪落 artifact |

---

## 已知边界

诚实列一下这套东西的真实短板：

1. **工具必须幂等**。崩在工具执行中间，续跑会重新执行一次：`write_file` 重写同样的内容无害；`edit_file` 会先判断编辑是否已经生效（new_string 已在、old_string 不再单独出现），生效了就不再替换，但删除型编辑（new_string 为空）判断不了；`bash` 不保证幂等。带副作用的工具（转账、发消息）需要自己做 idempotency key。
2. **token 是估算**，不是 tiktoken。口径写在 `context.estimate_tokens`，计费要按 provider 返回值算。
3. **摘要会渐进损失细节**。滚动合并时旧摘要会被截断，久远轮次的细粒度信息只存在 artifacts 和原始日志里。
4. **默认不并发**。`spawn_subagent` 是串行的；要并行得改 `_spawn`，注意同时写父日志需要加锁。
5. **审批门默认 fail closed**。写文件、编辑文件、bash 每次都要过审批门；没有配置回调时一律拒绝——这是安全的默认，但要记得配。CLI 交互模式逐条询问；`--yes` 或代码里的 `ApprovalGate.allow_all()` 才会全部放行。
6. **bash 不是沙箱**。工作目录在 workspace，但命令本身能访问整个文件系统；危险命令清单只是负向清单。环境变量按白名单传入（PATH、HOME、LANG 等），API key 不会传给子进程；确实需要的变量写进 `HARNESS_SHELL_ENV_ALLOW`（逗号分隔）。要真正隔离，请在容器里跑。

## 下一步可以做的

- `spawn_subagent` 并行化（`ThreadPoolExecutor` + 父日志串行化写入）
- 压缩摘要改走小模型（`compact_if_needed(..., summarize=fn)` 已经留好接口）
- 加一个 `--dry-run` 回放评测模式：给已有 `events.jsonl`，重跑对齐检查
- 把 `RunStats` 导出成 JSONL 指标，接评测

## 许可证

MIT，见 [LICENSE](LICENSE)。
