"""Frozen tasks and private answer keys for the Wiki net-benefit pilot."""

LOCATE = "locate"
TRACE = "trace"
IMPACT = "impact"
REPAIR = "repair"


def question(category, prompt, expected, *anchors):
    return {
        "category": category, "question": prompt, "expected": expected,
        "fields": {key: {str: "字符串", int: "整数", bool: "布尔值"}[type(value)]
                   for key, value in expected.items()},
        "evidence": [{"file": file, "needle": needle} for file, needle in anchors],
    }


QA = [
    question(LOCATE, "定位把耗时分位数换算成样本序号的函数。返回 file（仓库相对路径）和 function（函数名）。",
             {"file": "agent/usage.py", "function": "percentile"},
             ("agent/usage.py", "def percentile(")),
    question(LOCATE, "定位 SKILL.md 元数据读取的字节上限。返回 file 和 constant（常量名）。",
             {"file": "agent/skills.py", "constant": "MAX_HEADER_BYTES"},
             ("agent/skills.py", "MAX_HEADER_BYTES =")),
    question(LOCATE, "定位 OpenAI 兼容客户端实际发起请求的方法。返回 file 和 route（固定追加的 URL 路径，带开头斜杠）。",
             {"file": "agent/llm.py", "route": "/chat/completions"},
             ("agent/llm.py", "/chat/completions")),
    question(LOCATE, "定位 MCP Server 的响应适配器，它把 content 数组变成 ok 和文字。返回 file 和 function。",
             {"file": "agent/mcp.py", "function": "render_result"},
             ("agent/mcp.py", "def render_result(")),
    question(LOCATE, "定位会话对一条消息序列做 token 粗估的函数。返回 file 和 function。",
             {"file": "agent/context.py", "function": "estimate_messages_tokens"},
             ("agent/context.py", "def estimate_messages_tokens(")),
    question(LOCATE, "定位生成页面时查找 Git 仓库根目录的公开函数。返回 file 和 function。",
             {"file": "agent/wiki.py", "function": "plan_wiki"},
             ("agent/wiki.py", '"--show-toplevel"')),
    question(LOCATE, "定位负责把历史事件组装成旧轮次摘要的公开函数。返回 file 和 function。",
             {"file": "agent/compact.py", "function": "compact_if_needed"},
             ("agent/compact.py", "def compact_if_needed(")),
    question(LOCATE, "定位把调用标识映射成可稳定续跑的子代理 ID 的函数。返回 file 和 function。",
             {"file": "agent/subagent.py", "function": "sub_id_for_call"},
             ("agent/subagent.py", "def sub_id_for_call(")),
    question(LOCATE, "定位 Wiki 文本检索的默认命中条数。返回 file 与 default_limit（省略 limit 时的整数）。",
             {"file": "agent/wiki_mcp.py", "default_limit": 10},
             ("agent/wiki_mcp.py", '"limit", 10, 1, 20')),
    question(LOCATE, "定位运行截止时间检查的函数。返回 file 和 function。",
             {"file": "agent/context.py", "function": "deadline_exceeded"},
             ("agent/context.py", "def deadline_exceeded(")),
    question(TRACE, "一次模型调用成功后，运行中的累计 token 和离线 replay --usage 都从日志取数。"
                    "返回 event（共用事件类型）、live_reader（Session 中的方法名）、offline_reader（usage 模块函数名），"
                    "以及 offline_opens_session（离线统计是否打开 Session）。",
             {"event": "model/response", "live_reader": "stats", "offline_reader": "collect",
              "offline_opens_session": False},
             ("agent/loop.py", "session.append(EventType.MODEL_RESPONSE"),
             ("agent/session.py", "def stats("), ("agent/usage.py", "def collect(")),
    question(TRACE, "同一 run 的两轮模型响应复用了同一个工具调用 ID，且审批请求无 scope。"
                    "返回 gate（工具读取审批日志使用的类名）、key（区分两次请求的字段）、"
                    "uses_response_seq（字段值是否包含模型响应事件序号）和 shares_decision（第一次决定是否应用到第二次）。",
             {"gate": "JournalApproval", "key": "call_key", "uses_response_seq": True,
              "shares_decision": False},
             ("agent/loop.py", 'key = f"{response_seq}-{call.id}"'),
             ("agent/approval.py", 'data["call_key"] == self.call_key'), ("agent/approval.py", "same_scope")),
    question(TRACE, "CLI 首次启动带 Skills 的任务，之后继续该运行。"
                    "返回 discover（启动时加载目录的函数）、config_key（写入运行配置的键）、"
                    "register（启动和恢复都用的注册函数）和 tool_parallel（read_skill 是否为并行安全工具）。",
             {"discover": "discover_skills", "config_key": "skills", "register": "register_skills",
              "tool_parallel": True},
             ("agent/cli.py", 'extra["skills"] = skills'),
             ("agent/cli.py", 'register_skills(registry, cfg_dict.get("skills"))'),
             ("agent/skills.py", "read_skill, parallel_safe=True")),
    question(TRACE, "模型对一个工具提交了无法解码的 arguments。返回 call_error_flag（工具调用事件里的标记字段）、"
                    "result_ok（对应结果的 ok）、projected_arguments（后续历史里 assistant arguments 的实际字符串）和 abort_turn（是否关闭整轮）。",
             {"call_error_flag": "parse_error", "result_ok": False, "projected_arguments": "{}",
              "abort_turn": False},
             ("agent/loop.py", '"parse_error": True'), ("agent/session.py", "except json.JSONDecodeError")),
    question(TRACE, "自由文本回答没有工具调用时，主循环把它作为哪一种停止原因？"
                    "返回 reason（精确值）、resume_calls_model_again（续跑该 run 是否会再次请求模型）和 stop_event（结束事件类型）。",
             {"reason": "assistant_text", "resume_calls_model_again": False, "stop_event": "run/stop"},
             ("agent/loop.py", '"assistant_text"'), ("agent/events.py", 'RUN_STOP = "run/stop"'),
             ("tests/test_core.py", "def test_resume_is_idempotent_when_finished")),
    question(TRACE, "一次已注册的 MCP 调用得到了只有 structuredContent 的成功响应。"
                    "返回 adapter（渲染函数）、serialization（内容回灌的序列化格式，小写）和 ok（最终工具结果的成功布尔值）。",
             {"adapter": "render_result", "serialization": "json", "ok": True},
             ("agent/mcp.py", 'json.dumps(result["structuredContent"]'), ("agent/mcp.py", "ok, text = render_result")),
    question(TRACE, "观察到长工具结果被削短。返回 pointer（TOOL_RESULT 里持久化全文相对路径的字段），"
                    "read_tool（读取全文的工具），以及 projection_mentions_pointer（下一次投影是否向模型展示该指针）。",
             {"pointer": "artifact", "read_tool": "read_artifact", "projection_mentions_pointer": True},
             ("agent/loop.py", '"artifact": outcome.artifact'), ("agent/session.py", "完整结果见"),
             ("agent/tools.py", "def tool_read_artifact(")),
    question(TRACE, "上一轮留下三个可重放工具调用，进程重启时其中两个已有本轮结果，一个只有调用记录。"
                    "返回 pending_method（计算剩余调用的方法）、checked_event（能消除欠账的事件类型）、"
                    "pending_count（还需补几条）和 counts_tool_call_as_done（仅有调用记录是否算完成）。",
             {"pending_method": "_pending_calls", "checked_event": "tool/result", "pending_count": 1,
              "counts_tool_call_as_done": False},
             ("agent/loop.py", "def _pending_calls("), ("agent/loop.py", "if e.type == EventType.TOOL_RESULT"),
             ("agent/events.py", 'TOOL_RESULT = "tool/result"')),
    question(TRACE, "旧历史已经折叠成摘要，最新的模型请求仍要装配上下文。"
                    "返回 projector（事件转消息函数）、assembler（构造请求消息的函数）、summary_role（摘要消息的角色）和 first_protected_seq（滚动摘要绝不会覆盖的系统起始 seq）。",
             {"projector": "derive_messages", "assembler": "assemble", "summary_role": "user",
              "first_protected_seq": 0},
             ("agent/context.py", "messages = derive_messages(events)"),
             ("agent/session.py", "def _summary_message("), ("agent/compact.py", "first = 1")),
    question(TRACE, "同一个父调用中断后重新进入子代理执行。返回 id_factory（生成子 ID 的函数名）、"
                    "directory（直接容纳子运行目录的父目录名）和 reuse_same_call（父响应序号与调用 ID 均相同，是否找到同一 child run）。",
             {"id_factory": "sub_id_for_call", "directory": "subagents", "reuse_same_call": True},
             ("agent/loop.py", "sub_id_for_call("), ("agent/subagent.py", ' / "subagents"')),
    question(IMPACT, "计划修改 deadline 检查的启始时间。返回 checker（函数名）、"
                     "clock_event（当前使用的起始事件类型）、stop_reason（命中后主循环写入的 reason）、"
                     "persisted_budget_s（deadline_s 是否作为运行配置字段持久化）。",
             {"checker": "deadline_exceeded", "clock_event": "run/start", "stop_reason": "deadline",
              "persisted_budget_s": True},
             ("agent/context.py", "def deadline_exceeded("), ("agent/loop.py", 'return "deadline"'),
             ("agent/loop.py", '"budget": self.budget.as_dict()')),
    question(IMPACT, "准备允许修改已启用 Skill 的 description 后续跑。返回 validate（拒绝发生在哪个工具）、"
                     "persist_snapshot（已存在的 run 是否持有旧目录快照）、rebuild_system_on_resume（正常 resume 是否重建系统提示）和 related_test（覆盖渐进披露与续跑的测试方法名）。",
             {"validate": "read_skill", "persist_snapshot": True, "rebuild_system_on_resume": False,
              "related_test": "test_progressive_disclosure_and_resume_catalog"},
             ("agent/skills.py", "Skill 元数据已变化"), ("agent/cli.py", 'cfg_dict.get("skills")'),
             ("tests/test_week3.py", "def test_progressive_disclosure_and_resume_catalog")),
    question(IMPACT, "如果把工具消息折叠时保护的尾部结果数从 2 改成 3，"
                     "返回 first_file（首次组装与预算估计）、second_file（决定是否进一步生成摘要）、"
                     "output_role（被折叠的消息仍是什么角色）和 low_budget_test（低预算折叠测试方法）。",
             {"first_file": "agent/context.py", "second_file": "agent/compact.py", "output_role": "tool",
              "low_budget_test": "test_assemble_collapses_under_pressure"},
             ("agent/context.py", "reserved = 2"), ("agent/compact.py", "asm.over_budget"),
             ("tests/test_core.py", "def test_assemble_collapses_under_pressure")),
    question(IMPACT, "想把 Wiki MCP 的分页 offset 单位改成 UTF-8 字节。"
                     "返回 page（负责切页的方法名）、search（还要同步的检索方法名）、"
                     "total_field（响应里总长度字段）和 related_test（当前覆盖真实 stdio 分页的测试方法）。",
             {"page": "page", "search": "search", "total_field": "total_chars",
              "related_test": "test_real_stdio_tools_and_path_boundary"},
             ("agent/wiki_mcp.py", '"total_chars": len(markdown)'), ("agent/wiki_mcp.py", "offset += len(line) + 1"),
             ("tests/test_week3.py", "def test_real_stdio_tools_and_path_boundary")),
    question(IMPACT, "准备给 DeepSeek 换一种缓存命中 usage 字段。"
                     "返回 reader（离线统计读取缓存的函数）、cache_effects_live_total（只换缓存字段且 prompt_tokens 不变，是否影响在线累计 token）、"
                     "cache_aware_cost_method（使用命中与未命中分价的函数名）和 related_test（带价格断言的测试方法）。",
             {"reader": "_cache_hits", "cache_effects_live_total": False, "cache_aware_cost_method": "cost",
              "related_test": "test_tokens_cost_latency_and_tools"},
             ("agent/usage.py", "def _cache_hits("), ("agent/usage.py", "def cost("),
             ("agent/session.py", 'usage.get("prompt_tokens")'), ("tests/test_week1.py", "def test_tokens_cost_latency_and_tools")),
    question(IMPACT, "需要把 Wiki 生成的模型从离线换成真实模型，文件与外部来源保持相同。"
                     "返回 rerender（是否重新生成已有页）、metadata_field（记录写作模型的字段）、"
                     "author_has_tools（写正文的模型调用是否收到工具）和 related_test（验证该调用及 usage 的测试方法）。",
             {"rerender": True, "metadata_field": "model", "author_has_tools": False,
              "related_test": "test_optional_model_writes_only_prose_and_usage_is_logged"},
             ("agent/wiki.py", '(previous.get("provider"), previous.get("model")) != (provider, model)'),
             ("tests/test_wiki.py", "def test_optional_model_writes_only_prose_and_usage_is_logged")),
    question(IMPACT, "调整 Wiki 外部来源记录的字段结构。返回 parser_module（加载及校验来源清单的文件）、"
                     "primary_page（接收外部来源的 Wiki 页面）、stored_field（页面内隐藏元数据的字段）和"
                     "related_test（覆盖只更新依赖页并保留已采来源的测试方法）。",
             {"parser_module": "agent/wiki_sources.py", "primary_page": "dependencies.md", "stored_field": "web_sources",
              "related_test": "test_web_sources_update_only_dependencies_and_remain_pinned"},
             ("agent/wiki_sources.py", "def read_sources("), ("agent/wiki.py", 'name == "dependencies.md"'),
             ("tests/test_wiki.py", "def test_web_sources_update_only_dependencies_and_remain_pinned")),
    question(IMPACT, "准备调整并发工具调度的收集顺序。返回 scheduler（提交任务的 Harness 方法）、"
                     "write_completion_order（当前日志是否按线程完成先后写）、"
                     "order_test（明确断言结果顺序的测试方法）和 projection_consumer（从日志还原消息的函数）。",
             {"scheduler": "_run_batch", "write_completion_order": False,
              "order_test": "test_parallel_reads_run_concurrently_in_call_order", "projection_consumer": "derive_messages"},
             ("agent/loop.py", "futures = [pool.submit"), ("agent/session.py", "def derive_messages("),
             ("tests/test_week1.py", "def test_parallel_reads_run_concurrently_in_call_order")),
    question(IMPACT, "准备让子代理和父代理用同等 token 预算。返回 calculator（当前分配函数）、"
                     "changes_input_cap（现有算法是否降低 max_input_tokens）、changes_total_cap（是否存在降低 max_total_tokens 的分支）和"
                     "caller_module（实际装配子运行配置的文件）。",
             {"calculator": "child_budget", "changes_input_cap": False, "changes_total_cap": True,
              "caller_module": "agent/loop.py"},
             ("agent/subagent.py", "max_input_tokens=parent.max_input_tokens"),
             ("agent/subagent.py", "parent.max_total_tokens // 4"), ("agent/loop.py", "budget=child_budget(")),
    question(IMPACT, "调整损坏尾行恢复时的原始字节备份。返回 journal_module（物理截断逻辑的文件）、"
                     "backup_write_mode（备份文件的 Python open 模式）、"
                     "tail_test（重开日志验证修复的测试方法）和 affects_valid_midstream_events（改动是否应该允许截断坏行后的正常事件）。",
             {"journal_module": "agent/events.py", "backup_write_mode": "ab",
              "tail_test": "test_tail_half_line_is_truncated_on_disk", "affects_valid_midstream_events": False},
             ("agent/events.py", 'open(self.backup_path, "ab")'),
             ("tests/test_regressions.py", "def test_tail_half_line_is_truncated_on_disk"),
             ("tests/test_regressions.py", "def test_mid_file_corruption_is_refused")),
]


def repair(prompt, file, test, anchor, mutation):
    return {"category": REPAIR, "question": prompt, "file": file,
            "test": test, "anchor": anchor, "mutation": mutation}


BUGS = [
    repair("连续读取很多长结果的任务在输入压力下仍然没有折叠旧结果。恢复输入预算超过阈值时的旧结果折叠，保留最新两条。",
           "agent/context.py", "test_core.TestContext.test_assemble_collapses_under_pressure",
           "    reserved = 2", "    reserved = 1000"),
    repair("压缩之后，旧工具全文仍在磁盘但摘要没有保留首次压缩那批结果的引用。恢复证据引用，并保留滚动摘要行为。",
           "agent/compact.py", "test_core.TestCompaction.test_compaction_keeps_system_and_pointers",
           "    artifacts = list(artifacts)", "    artifacts = []"),
    repair("恢复的历史对话在模型协议校验失败，已经是 JSON 对象字符串的工具参数被二次编码。修复该投影行为。",
           "agent/session.py", "test_regressions.TestToolArguments.test_arguments_are_replayed_verbatim",
           '        return args if isinstance(parsed, dict) else "{}"',
           '        return json.dumps(args) if isinstance(parsed, dict) else "{}"'),
    repair("DeepSeek 模型请求的 prompt cache 命中已存在于 usage，却未计入离线报告。修复命中统计并维持 OpenAI 格式兼容。",
           "agent/usage.py", "test_week1.TestUsage.test_tokens_cost_latency_and_tools",
           '        return usage["prompt_cache_hit_tokens"]', "        return 0"),
    repair("恢复一个已经完成的局部替换时，若新文本包含旧文本，替换被重复应用。维持已有参数错误规则并修复重放行为。",
           "agent/tools.py", "test_regressions.TestEditFileIdempotent.test_replaying_edit_whose_new_contains_old",
           '    if new and new != old and new in text and text.replace(new, "\\0").count(old) == 0:',
           "    if new and new != old and new in text and old not in text:"),
    repair("开启工具调用预算后，任务连续调用工具仍继续运行。恢复预算为 3 时的预算终止，并维持既有停止原因。",
           "agent/loop.py", "test_core.TestBudgetAndStop.test_tool_call_budget",
           "        if stats.tool_calls >= budget.max_tool_calls:",
           "        if stats.tool_calls >= budget.max_turns:"),
    repair("MCP 图片响应不再能让模型看到文件类型提示。恢复对 image/audio 的简短说明，并避免把二进制内容灌进上下文。",
           "agent/mcp.py", "test_week1.TestMCPClient.test_results_errors_and_images",
           "            parts.append(f\"[{kind} {item.get('mimeType', '')}，约 {size} 字节，未放入上下文]\")",
           "            parts.append(\"[media]\")"),
    repair("启动时启用两个 Skill 后，系统提示无法告知模型已选 Skills 的名称和描述。恢复元数据目录并保留按需读取全文的机制。",
           "agent/skills.py", "test_week3.TestSkills.test_progressive_disclosure_and_resume_catalog",
           '    return (f"{system}\\n\\n可用 Skills\\n{rows}\\n"',
           '    return (f"{system}\\n\\n可用 Skills\\n"'),
    repair("将带未提交修改的文件写入 Wiki，再把源文件恢复到 Git HEAD 后，Wiki 仍保留旧的修改内容。修复此增量更新场景。",
           "agent/wiki.py", "test_wiki.TestRepositoryWiki.test_only_affected_page_updates_and_dirty_snapshot_can_revert",
           '            candidates = diffs[base] | set(previous["dirty"]) | (paths - old_sources.keys())',
           '            candidates = diffs[base] | (paths - old_sources.keys())'),
    repair("事件日志末尾半条 JSON 截断后能继续写入，但事后备份找不到截断前的残缺字节。修复尾行恢复备份。",
           "agent/events.py", "test_regressions.TestEventLogTailRecovery.test_tail_half_line_is_truncated_on_disk",
           "                bak.write(data[good_end:])", "                bak.write(data[:good_end])"),
]

CASES = [{**case, "id": f"W{index:02d}"} for index, case in enumerate(QA + BUGS, 1)]
