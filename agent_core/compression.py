"""压缩与记忆写入 hooks（v1.1 阶段 4）。

压缩只改“进上下文的表示”，原文由宿主存储保留；Agent 在执行器结束后统一提交
memory_delta。这里不依赖任何具体模型/存储实现。
"""
from __future__ import annotations

from .domain import (
    BudgetProfile, CompressionResult, Evidence, ItemDraft, Message, RunContext,
    RunState,
)

COMPACT_SYSTEM = """把以下对话压成可继续工作的中文摘要。必须保留：
1 目标与验收；2 已确定的事实；3 已完成的动作；4 踩过的坑；
5 已排除的方案；6 用户原话与约束；7 未决问题；8 下一步。
只保留事实，不编造；直接输出摘要正文。"""


class Compressor:
    def __init__(self, model, *, keep_recent: int = 4, max_summary_chars: int = 600):
        self.model = model
        self.keep_recent = max(1, int(keep_recent))
        self.max_summary_chars = max(60, int(max_summary_chars))

    def compress(self, messages: list[Message],
                 budget: BudgetProfile | None = None) -> CompressionResult:
        if len(messages) <= self.keep_recent:
            return CompressionResult(False, removed_messages=0,
                                     kept_messages=len(messages))
        older = messages[:-self.keep_recent]
        text = "\n".join(f"{m.role}: {m.content}" for m in older)
        resp = self.model.complete(
            [Message("system", COMPACT_SYSTEM), Message("user", text)],
            [], budget or BudgetProfile(max_steps=1, max_tokens=2000))
        summary = (resp.content or "").strip()
        return CompressionResult(
            compressed=True, summary=summary,
            removed_messages=len(older), kept_messages=self.keep_recent,
            over_limit=len(summary) > self.max_summary_chars)

    def apply(self, messages: list[Message], result: CompressionResult) -> list[Message]:
        if not result.compressed:
            return list(messages)
        return [Message("system", "历史摘要（原文可回查）：\n" + result.summary)] \
            + list(messages[-self.keep_recent:])


class MemoryHooks:
    """压缩 + 把工具结果/摘要转成 ItemDraft；落库由 Agent 统一提交。"""

    def __init__(self, compressor: Compressor):
        self.compressor = compressor
        self._seen_calls: set[tuple[str, str]] = set()

    def before_model_call(self, ctx: RunContext, state: RunState) -> None:
        return None

    def after_tool_batch(self, ctx: RunContext, state: RunState) -> None:
        for tr in state.tool_results:
            key = (ctx.run_id, tr.call_id)
            if key in self._seen_calls or not tr.ok:
                continue
            self._seen_calls.add(key)
            state.memory_delta.append(ItemDraft(
                content=f"{tr.name}: {tr.content[:1000]}",
                kind="fact", domain="memory",
                title=f"工具结果 {tr.name}",
                summary=tr.content[:200],
                evidence=(Evidence("trace", f"tool:{tr.call_id}"),)))

    def on_context_pressure(self, ctx: RunContext, state: RunState) -> None:
        res = self.compressor.compress(state.messages, ctx.budget)
        if not res.compressed:
            return
        state.messages = self.compressor.apply(state.messages, res)
        state.summary = res.summary
        state.memory_delta.append(ItemDraft(
            content=res.summary, kind="fact", domain="memory",
            title="会话摘要", summary=res.summary[:200],
            evidence=(Evidence("trace", f"run:{ctx.run_id}"),)))

    def before_finish(self, ctx: RunContext, state: RunState) -> None:
        return None

    def on_cancel(self, ctx: RunContext, state: RunState) -> None:
        return None
