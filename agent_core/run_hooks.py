"""RunHooks 默认实现：v1 先提供 no-op；阶段 4 由 MemoryHooks 接管。"""
from __future__ import annotations

from .domain import RunContext, RunState


class NoopRunHooks:
    def before_model_call(self, ctx: RunContext, state: RunState) -> None:
        return None

    def after_tool_batch(self, ctx: RunContext, state: RunState) -> None:
        return None

    def on_context_pressure(self, ctx: RunContext, state: RunState) -> None:
        return None

    def before_finish(self, ctx: RunContext, state: RunState) -> None:
        return None

    def on_cancel(self, ctx: RunContext, state: RunState) -> None:
        return None
