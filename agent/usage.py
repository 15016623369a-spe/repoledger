"""成本与耗时统计：把一个 run（含全部子 agent）的日志汇总成一张账单。

只读日志文件，不经过 Session（Session 打开时可能截断坏行、追加提示，统计不该改日志）。

口径：
  · token 取模型接口返回的 usage，不用本地估算；
  · 费用 = 单价 × token，单价由调用方提供（不同模型、不同时段价格不同，不写死）；
  · 缓存命中的输入 token 兼容两种写法：DeepSeek 的 prompt_cache_hit_tokens、
    OpenAI 的 prompt_tokens_details.cached_tokens；
  · 耗时分位数用 nearest-rank：排序后取第 ceil(p × n) 个。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Pricing:
    """每百万 token 的单价。没配置就只统计 token，不估算费用。"""

    input_per_m: float | None = None
    output_per_m: float | None = None
    cache_hit_input_per_m: float | None = None   # 不填则按普通输入价计
    currency: str = "USD"

    @classmethod
    def from_env(cls) -> "Pricing":
        def num(name: str) -> float | None:
            raw = os.environ.get(name)
            try:
                return float(raw) if raw else None
            except ValueError:
                return None
        return cls(num("HARNESS_PRICE_IN"), num("HARNESS_PRICE_OUT"),
                   num("HARNESS_PRICE_CACHE_HIT"),
                   os.environ.get("HARNESS_PRICE_CURRENCY") or "USD")

    @property
    def configured(self) -> bool:
        return self.input_per_m is not None and self.output_per_m is not None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


@dataclass
class ToolUsage:
    calls: int = 0
    errors: int = 0
    durations_ms: list[float] = field(default_factory=list)

    @property
    def total_ms(self) -> float:
        return sum(self.durations_ms)


@dataclass
class RunUsage:
    run_id: str
    depth: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit_tokens: int = 0
    usage_missing: int = 0            # 接口没返回 usage 的模型调用数
    model_latency_ms: list[float] = field(default_factory=list)
    tools: dict[str, ToolUsage] = field(default_factory=dict)
    parallel_batches: int = 0
    wall_s: float | None = None
    stop_reason: str | None = None
    children: list["RunUsage"] = field(default_factory=list)

    # ---------- 含子 agent 的合计 ----------
    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def total(self, attr: str) -> int:
        return sum(getattr(u, attr) for u in self.walk())

    def all_model_latency(self) -> list[float]:
        return [ms for u in self.walk() for ms in u.model_latency_ms]

    def all_tools(self) -> dict[str, ToolUsage]:
        merged: dict[str, ToolUsage] = {}
        for u in self.walk():
            for name, t in u.tools.items():
                m = merged.setdefault(name, ToolUsage())
                m.calls += t.calls
                m.errors += t.errors
                m.durations_ms.extend(t.durations_ms)
        return merged

    def cost(self, pricing: Pricing) -> float | None:
        if not pricing.configured:
            return None
        hit = self.total("cache_hit_tokens")
        miss = self.total("tokens_in") - hit
        hit_price = (pricing.cache_hit_input_per_m if pricing.cache_hit_input_per_m is not None
                     else pricing.input_per_m)
        return (miss * pricing.input_per_m + hit * hit_price
                + self.total("tokens_out") * pricing.output_per_m) / 1_000_000


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break   # 尾部半行：和 EventLog 同一口径，到此为止
    return events


def _cache_hits(usage: dict[str, Any]) -> int:
    if isinstance(usage.get("prompt_cache_hit_tokens"), int):
        return usage["prompt_cache_hit_tokens"]
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    return 0


def _seconds_between(start: str, end: str) -> float | None:
    from datetime import datetime
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except ValueError:
        return None


def collect(run_dir: Path, *, depth: int = 0) -> RunUsage:
    """汇总一个 run 目录，递归包含 subagents/ 下的子 agent。"""
    run_dir = Path(run_dir)
    events = _read_events(run_dir / "events.jsonl")
    u = RunUsage(run_id=run_dir.name, depth=depth)
    names: dict[str, str] = {}
    for ev in events:
        data = ev.get("data") or {}
        kind = ev.get("type")
        if kind == "model/response":
            u.model_calls += 1
            usage = data.get("usage") or {}
            if isinstance(usage.get("prompt_tokens"), int):
                u.tokens_in += usage["prompt_tokens"]
                u.tokens_out += int(usage.get("completion_tokens") or 0)
                u.cache_hit_tokens += _cache_hits(usage)
            else:
                u.usage_missing += 1
            if isinstance(data.get("latency_ms"), (int, float)):
                u.model_latency_ms.append(float(data["latency_ms"]))
        elif kind == "tool/call":
            names[data.get("call_id")] = data.get("name") or "?"
        elif kind == "tool/result":
            t = u.tools.setdefault(names.get(data.get("call_id"), "?"), ToolUsage())
            t.calls += 1
            if not data.get("ok", True):
                t.errors += 1
            if isinstance(data.get("elapsed_ms"), (int, float)):
                t.durations_ms.append(float(data["elapsed_ms"]))
        elif kind == "run/stop":
            u.stop_reason = data.get("reason")
    # 一批 n 个并行调用写 n 条带 parallel 标记的 tool/call，换算成批数
    u.parallel_batches = sum(
        1 for i, ev in enumerate(events)
        if ev.get("type") == "tool/call" and (ev.get("data") or {}).get("parallel")
        and not (i > 0 and events[i - 1].get("type") == "tool/call"
                 and (events[i - 1].get("data") or {}).get("parallel")))
    if events:
        u.wall_s = _seconds_between(events[0].get("ts", ""), events[-1].get("ts", ""))
    sub_root = run_dir / "subagents"
    if sub_root.is_dir():
        for child in sorted(p for p in sub_root.iterdir() if p.is_dir()):
            u.children.append(collect(child, depth=depth + 1))
    return u


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1000:.2f}s" if value >= 1000 else f"{value:.0f}ms"


def format_report(u: RunUsage, pricing: Pricing | None = None) -> str:
    pricing = pricing or Pricing()
    lines = [f"run {u.run_id}  结束原因: {u.stop_reason or '未结束'}"
             + (f"  墙钟 {u.wall_s}s" if u.wall_s is not None else "")]
    latency = u.all_model_latency()
    lines.append(
        f"模型  调用 {u.total('model_calls')} 次 · 输入 {u.total('tokens_in')} tok"
        f"（缓存命中 {u.total('cache_hit_tokens')}）· 输出 {u.total('tokens_out')} tok"
        f" · 延迟 P50 {_fmt_ms(percentile(latency, .5))} / P95 {_fmt_ms(percentile(latency, .95))}")
    missing = u.total("usage_missing")
    if missing:
        lines.append(f"      其中 {missing} 次调用接口没有返回 usage，token 与费用偏低")
    cost = u.cost(pricing)
    lines.append(f"费用  {cost:.4f} {pricing.currency}" if cost is not None
                 else "费用  未配置单价（HARNESS_PRICE_IN / HARNESS_PRICE_OUT，每百万 token）")
    tools = u.all_tools()
    if tools:
        lines.append("工具  名称                         次数  失败   总耗时     P95")
        for name, t in sorted(tools.items(), key=lambda kv: -kv[1].total_ms):
            lines.append(f"      {name[:28]:<28} {t.calls:>4} {t.errors:>5} {_fmt_ms(t.total_ms):>9}"
                         f" {_fmt_ms(percentile(t.durations_ms, .95)):>7}")
    batches = u.total("parallel_batches")
    if batches:
        lines.append(f"并行  {batches} 批只读调用并发执行")
    if u.children:
        lines.append("子 agent")
        for child in u.walk():
            if child is u:
                continue
            lines.append(f"      {'  ' * (child.depth - 1)}{child.run_id}  调用 {child.model_calls} 次"
                         f" · {child.tokens_in + child.tokens_out} tok · {child.stop_reason or '未结束'}")
    return "\n".join(lines)
