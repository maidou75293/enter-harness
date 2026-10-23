"""Hooks 管理器。

事件（字符串常量，业务层可自定义新事件）：
    run_start / run_end / step_start / step_end
    model_start / model_end
    before_tool / after_tool / tool_error
    error / interrupt

约定：
- 普通事件：emit(event, **payload) 顺序调用，返回值忽略。
- before_tool：钩子可返回 dict —— 其中的 `args` 覆盖工具参数；返回 {"deny": "原因"}
  或 raise HookDenied 会直接拒绝本次调用。
- after_tool：可返回 dict 覆盖执行结果中的 `result`。
- 钩子失败：默认隔离（记录后继续），除非 `strict=True`。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

HOOK_EVENTS = (
    "run_start", "run_end", "step_start", "step_end",
    "model_start", "model_end",
    "before_tool", "after_tool", "tool_error",
    "error", "interrupt",
)


class HookDenied(Exception):
    """钩子主动拒绝（比策略 deny 更早）。"""

    def __init__(self, reason: str = "hook denied"):
        super().__init__(reason)
        self.reason = reason


@dataclass(order=True)
class HookSpec:
    priority: int
    seq: int
    event: str = field(compare=False)
    fn: Callable = field(compare=False)


class HookManager:
    def __init__(self, strict: bool = False):
        self.strict = strict
        self._hooks: dict[str, list[HookSpec]] = {}
        self._seq = 0

    # ---------------- 注册 ----------------
    def register(self, event: str, fn: Callable, priority: int = 0) -> Callable:
        self._seq += 1
        self._hooks.setdefault(event, []).append(HookSpec(priority, self._seq, event, fn))
        self._hooks[event].sort()
        return fn

    def on(self, event: str, priority: int = 0):
        """装饰器：@hooks.on("before_tool")。"""
        def deco(fn):
            self.register(event, fn, priority)
            return fn
        return deco

    def off(self, event: str, fn: Callable) -> None:
        self._hooks[event] = [h for h in self._hooks.get(event, []) if h.fn is not fn]

    def clear(self, event: str | None = None) -> None:
        if event is None:
            self._hooks.clear()
        else:
            self._hooks.pop(event, None)

    def list(self, event: str | None = None) -> list:
        if event is not None:
            return [h.fn for h in self._hooks.get(event, [])]
        return {k: [h.fn for h in v] for k, v in self._hooks.items()}

    # ---------------- 触发 ----------------
    @staticmethod
    async def _call(fn: Callable, **payload):
        r = fn(**payload)
        if inspect.isawaitable(r):
            r = await r
        return r

    async def emit(self, event: str, **payload) -> list:
        out = []
        for h in list(self._hooks.get(event, [])):
            try:
                out.append(await self._call(h.fn, **payload))
            except HookDenied:
                raise
            except Exception as e:  # noqa: BLE001 —— 钩子不应拖垮主流程
                if self.strict:
                    raise
                log.warning("hook %s on %s failed: %s", getattr(h.fn, "__name__", h.fn), event, e)
        return out

    async def before_tool(self, tool: str, args: dict, ctx: Any = None) -> dict:
        """返回（可能被修改的）args；被拒绝时抛 HookDenied。"""
        cur = dict(args or {})
        for h in list(self._hooks.get("before_tool", [])):
            try:
                r = await self._call(h.fn, tool=tool, args=cur, ctx=ctx)
            except HookDenied:
                raise
            except Exception as e:  # noqa: BLE001
                if self.strict:
                    raise
                log.warning("before_tool hook failed: %s", e)
                continue
            if isinstance(r, dict):
                if r.get("deny") is not None:
                    raise HookDenied(str(r.get("deny") or "hook denied"))
                if isinstance(r.get("args"), dict):
                    cur = dict(r["args"])
        return cur

    async def after_tool(self, tool: str, args: dict, result: dict, ctx: Any = None) -> dict:
        cur = result
        for h in list(self._hooks.get("after_tool", [])):
            try:
                r = await self._call(h.fn, tool=tool, args=args, result=cur, ctx=ctx)
            except Exception as e:  # noqa: BLE001
                if self.strict:
                    raise
                log.warning("after_tool hook failed: %s", e)
                continue
            if isinstance(r, dict):
                cur = {**cur, **r}
        return cur
