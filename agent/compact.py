"""压缩：先剪枝，再摘要（§3.5，只做两档）。

第一档（剪枝）在 context.assemble 里做：确定性折叠工具结果，不调模型。
第二档（摘要）在这里做：把"已完成且尚未被折叠的旧轮次"合成一条摘要事件。

三条铁律，缺一条就会把证据链搞断：
  1. 只摘要"剪枝之后仍然超预算"的情况 —— 否则就是压缩风暴；
  2. 已经被摘要覆盖的轮次不再参与摘要 —— 否则摘要会自我滚雪球；
  3. 摘要事件必须带 covers 指针，且区间里已有的旧摘要要**原文并入**，
     否则"摘要的摘要"会静默丢信息。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .context import Budget, assemble, estimate_tokens
from .events import Event, EventType
from .session import Session, _shadowed_seqs

# 摘要文本自身的大小上限，防止摘要变成新的膨胀源
MAX_SUMMARY_TOKENS = 600
# 每条素材的截断长度
ITEM_CHARS = 180
# 至少保留最近 N 轮不折叠
DEFAULT_KEEP_RECENT = 3


@dataclass
class CompactPolicy:
    trigger_ratio: float = 0.70     # 兜底阈值（assemble 已判 over_budget 时才会走到）
    keep_recent: int = DEFAULT_KEEP_RECENT

    def should_compact(self, session: Session, budget: Budget) -> bool:
        """只有"剪无可剪仍超预算"才值得付出摘要的代价。"""
        asm = assemble(session, budget)
        return asm.over_budget and \
            asm.est_tokens > int(budget.max_input_tokens * self.trigger_ratio)


def _turns(events: list[Event]) -> dict[int, list[Event]]:
    out: dict[int, list[Event]] = {}
    for ev in events:
        out.setdefault(ev.turn, []).append(ev)
    return out


def _turn_complete(evs: list[Event]) -> bool:
    """一轮算完整：有模型响应，且每个 tool/call 都有对应 tool/result。"""
    if not any(e.type == EventType.MODEL_RESPONSE and e.data.get("tool_calls") for e in evs):
        return False
    called = {e.data.get("call_id") for e in evs if e.type == EventType.TOOL_CALL}
    done = {e.data.get("call_id") for e in evs if e.type == EventType.TOOL_RESULT}
    return called <= done


def _digest(evs: list[Event]) -> tuple[str, list[str]]:
    """把若干轮压成可读摘要素材，并收集 artifact 指针。"""
    lines: list[str] = []
    artifacts: list[str] = []
    for ev in evs:
        if ev.type == EventType.MODEL_RESPONSE:
            text = (ev.data.get("text") or "").strip()
            if text:
                lines.append(f"· 判断: {text[:ITEM_CHARS]}")
            for c in ev.data.get("tool_calls") or []:
                lines.append(f"· 调用 {c.get('name')}({str(c.get('args'))[:120]})")
        elif ev.type == EventType.TOOL_RESULT:
            ok = "成功" if ev.data.get("ok", True) else "失败"
            body = (ev.data.get("content") or "").replace("\n", " ")[:ITEM_CHARS]
            lines.append(f"· 工具{ok}: {body}")
            if ev.data.get("artifact"):
                artifacts.append(str(ev.data["artifact"]))
        elif ev.type == EventType.GUARD_DENY:
            lines.append(f"· 守卫拒绝 {ev.data.get('guard')}: {ev.data.get('reason')}")
        elif ev.type == EventType.COMPACT_SUMMARY:
            # 铁律 3：旧摘要被并入新摘要时必须原样保留，否则信息静默丢失
            lines.append(f"· 更早的摘要(seq {ev.data.get('covers')}): {ev.data.get('text', '')[:400]}")
            artifacts.extend(ev.data.get("artifacts") or [])
    return "\n".join(lines), artifacts


def _trim_to_tokens(text: str, limit: int) -> str:
    """按 **token 口径** 截断（中文 1 字≈1 token，不能按字符数估算）。"""
    if estimate_tokens(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    nl = cut.rfind("\n")
    if nl > lo * 0.6:
        cut = cut[:nl]
    return cut + "\n…（摘要超出预算已截断，完整内容见原始事件与 artifacts）"


def compact_if_needed(
    session: Session,
    budget: Budget,
    *,
    policy: CompactPolicy | None = None,
    summarize: Callable[[str, str], str] | None = None,
) -> bool:
    """需要就把最旧的若干完整轮折叠成一条摘要事件。返回是否发生了压缩。

    summarize 可选，签名 (digest, task) -> summary。
    默认用确定性抽取式摘要，保证无模型时行为完全可复现。
    """
    policy = policy or CompactPolicy()
    if not policy.should_compact(session, budget):
        return False

    events = session.events
    shadowed = _shadowed_seqs(events)
    turns = _turns(events)

    # 铁律 2：已经被覆盖过的轮次不再参与（否则摘要滚雪球）
    candidates = sorted(
        t for t, evs in turns.items()
        if t >= 1 and _turn_complete(evs) and not any(e.seq in shadowed for e in evs)
    )
    foldable = candidates[:-policy.keep_recent] if len(candidates) > policy.keep_recent else []
    if not foldable:
        return False

    # 上一条仍然生效的摘要：它的内容必须被原文带进新摘要，保证滚动合并而非层层追加
    prev: Event | None = None
    for ev in reversed(events):
        if ev.type == EventType.COMPACT_SUMMARY and ev.seq not in shadowed:
            prev = ev
            break

    chosen: list[Event] = []
    for t in foldable:
        chosen.extend(e for e in turns[t] if e.type != EventType.COMPACT_SUMMARY)
    chosen.sort(key=lambda e: e.seq)
    if not chosen:
        return False

    # 关键：covers 始终从 seq=1 开始，形成"一条滚动摘要覆盖全部早期历史"。
    # seq 0 是 run/start（system 提示词），永不覆盖；原目标由摘要正文重述。
    first = 1
    last = max(chosen[-1].seq, prev.seq if prev else 0)
    if last < 2:
        return False

    new_digest, artifacts = _digest([e for e in chosen if first <= e.seq <= last])
    artifacts = list(artifacts)
    if prev is not None:
        artifacts.extend(prev.data.get("artifacts") or [])
    if not new_digest.strip() and prev is None:
        return False

    task = session.task()
    if summarize:
        text = summarize(new_digest, task)
    else:
        # 顺序刻意如此：目标 → 证据指针 → 新内容 → 旧摘要。
        # 超预算时从尾部截断，先牺牲最旧的信息，目标和指针永远保得住。
        head = [f"原始目标: {task}"]
        if artifacts:
            head.append("证据文件: " + ", ".join(sorted(set(artifacts))))
        head.append(f"已折叠至第 {foldable[-1]} 轮（原始事件仍在日志中，可按 seq 回查）")
        body = ["· 本阶段新增:", new_digest]
        if prev is not None:
            prev_text = str(prev.data.get("text") or "")
            body += ["", f"· 更早阶段摘要（原 seq {prev.data.get('covers')}，可能已被截断）:", prev_text]
        text = "\n".join(head + body)

    text = _trim_to_tokens(text, MAX_SUMMARY_TOKENS)

    session.append(EventType.COMPACT_SUMMARY, {
        "covers": [first, last],
        "turns": [foldable[0], foldable[-1]],
        "text": text,
        "originals": [e.seq for e in chosen],
        "artifacts": sorted(set(artifacts)),
        "rolling": prev is not None,
    }, turn=session.log.last_turn)
    return True
