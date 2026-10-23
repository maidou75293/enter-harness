"""`ProviderModelPort`：把 async provider 适配成内核使用的同步 `ModelPort`。

内核的 `Agent`/执行器是同步的，而模型调用普遍是 async（httpx）。
本适配器负责：

- 消息：`Message` / dict → OpenAI 消息 dict；
- 工具：`Tool` / dict → `{"type": "function", ...}`；
- 调用：无事件循环时 `asyncio.run`；已在事件循环里则切独立线程
  （复制 contextvars，避免丢失租户/会话上下文）；
- 回包：assistant dict → `ModelResponse`（content / tool_calls / usage）。

逐 token 流式仍由 provider 的 `chat_stream` 提供；`ModelPort.stream`
按整段返回一个 `ModelDelta`，保证端口契约可用。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import queue
import threading
from typing import Any, Iterator, Sequence

from ..domain import Message, ModelDelta, ModelResponse, ToolCall, Usage


def _run_sync(coro):
    """在同步代码里跑协程；已在事件循环中时用独立线程 + contextvars。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    ctx = contextvars.copy_context()
    box: dict[str, Any] = {}

    def runner():
        try:
            box["value"] = ctx.run(asyncio.run, coro)
        except BaseException as e:  # noqa: BLE001 —— 原样回抛
            box["error"] = e

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


_END = object()


def _iter_sync(make_agen) -> Iterator[Any]:
    """把异步生成器变成同步生成器：后台线程跑事件循环，队列转发。"""
    q: queue.Queue = queue.Queue()
    ctx = contextvars.copy_context()

    def run():
        async def pump():
            try:
                async for item in make_agen():
                    q.put(item)
            except BaseException as e:  # noqa: BLE001 —— 在消费方原样抛出
                q.put(e)
            finally:
                q.put(_END)

        ctx.run(asyncio.run, pump())

    threading.Thread(target=run, daemon=True).start()
    while True:
        item = q.get()
        if item is _END:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def _message_dict(m) -> dict:
    if isinstance(m, dict):
        return dict(m)
    if hasattr(m, "as_dict"):
        return m.as_dict()
    return {"role": getattr(m, "role", "user"), "content": str(m)}


def _tool_schema(t) -> dict:
    if isinstance(t, dict):
        if "function" in t:
            return t
        if "name" in t:
            return {"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }}
        return t
    return {"type": "function", "function": {
        "name": getattr(t, "name", ""),
        "description": getattr(t, "description", ""),
        "parameters": getattr(t, "parameters", None) or {"type": "object", "properties": {}},
    }}


def _tool_calls(items) -> tuple[ToolCall, ...]:
    out = []
    for i, c in enumerate(items or []):
        fn = c.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append(ToolCall(id=str(c.get("id") or f"call_{i}"),
                            name=str(fn.get("name") or ""), arguments=args))
    return tuple(out)


def _merge_usage(usage: dict | None) -> Usage:
    usage = usage or {}
    return Usage(input_tokens=int(usage.get("prompt_tokens") or 0),
                 output_tokens=int(usage.get("completion_tokens") or 0))


def _merge_tool_calls(calls: dict, items) -> None:
    """把流式 chunk 里的 tool_calls 按 index 聚合（arguments 可能是碎片）。"""
    for i, c in enumerate(items or []):
        idx = int(c.get("index", i) or 0)
        cur = calls.setdefault(idx, {"id": "", "type": "function",
                                     "function": {"name": "", "arguments": ""}})
        if c.get("id"):
            cur["id"] = c["id"]
        fn = c.get("function") or {}
        if fn.get("name"):
            cur["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            cur["function"]["arguments"] += fn["arguments"]


class ProviderModelPort:
    """实现 `ModelPort`，底层是 async provider。

    `on_delta` 用于边生成边推事件（SSE）：设置后 `complete()` 改走
    `provider.chat_stream()`，逐块回调增量文本，结束时聚合出完整
    `ModelResponse`；不设置则仍走 `provider.chat()`，行为不变。
    """

    def __init__(self, provider, on_delta=None):
        self.provider = provider
        self.on_delta = on_delta

    # ---------------- 内部 ----------------
    def _provider_stream(self, messages, tools):
        """异步迭代 provider.chat_stream()；provider 不支持时回退一次性结果。"""
        if not hasattr(self.provider, "chat_stream"):
            out = _run_sync(self.provider.chat(
                [_message_dict(m) for m in messages],
                tools=[_tool_schema(t) for t in tools] or None))
            if not isinstance(out, dict):
                raise TypeError(
                    f"provider.chat 必须返回 dict，实际是 {type(out).__name__}")
            yield {"content": out.get("content") or "",
                   "tool_calls": out.get("tool_calls"),
                   "usage": out.get("usage"),
                   "finish_reason": out.get("finish_reason") or "stop",
                   "done": True}
            return
        msgs = [_message_dict(m) for m in messages]
        schemas = [_tool_schema(t) for t in tools] or None
        yield from _iter_sync(lambda: self.provider.chat_stream(msgs, schemas))

    # ---------------- ModelPort ----------------
    def complete(self, messages: Sequence[Message], tools: Sequence[dict] = (),
                 budget=None) -> ModelResponse:
        if self.on_delta is None:
            out = _run_sync(self.provider.chat(
                [_message_dict(m) for m in messages],
                tools=[_tool_schema(t) for t in tools] or None))
            if not isinstance(out, dict):
                raise TypeError(
                    f"provider.chat 必须返回 dict，实际是 {type(out).__name__}")
            return ModelResponse(
                content=str(out.get("content") or ""),
                tool_calls=_tool_calls(out.get("tool_calls")),
                usage=_merge_usage(out.get("usage")),
                finish_reason=str(out.get("finish_reason") or "stop"))

        parts: list[str] = []
        calls: dict = {}
        usage = Usage()
        finish = ""
        for chunk in self._provider_stream(messages, tools):
            text = chunk.get("content") or ""
            if text:
                parts.append(text)
                self.on_delta(text)
            _merge_tool_calls(calls, chunk.get("tool_calls"))
            if chunk.get("usage"):
                usage = _merge_usage(chunk.get("usage"))
            if chunk.get("finish_reason"):
                finish = str(chunk["finish_reason"])
        return ModelResponse(content="".join(parts),
                             tool_calls=_tool_calls([calls[k] for k in sorted(calls)]),
                             usage=usage, finish_reason=finish or "stop")

    def stream(self, messages: Sequence[Message], tools: Sequence[dict] = (),
               budget=None) -> Iterator[ModelDelta]:
        """逐块产出 `ModelDelta`。

        `tool_calls` 是**累计值**（按 index 聚合），最后一个 delta 即完整列表；
        没有 `chat_stream` 的 provider 退化为一次性结果。
        """
        calls: dict = {}
        for chunk in self._provider_stream(messages, tools):
            _merge_tool_calls(calls, chunk.get("tool_calls"))
            yield ModelDelta(
                content=chunk.get("content") or "",
                usage=_merge_usage(chunk.get("usage")),
                finish_reason=str(chunk.get("finish_reason") or ""),
                tool_calls=_tool_calls([calls[k] for k in sorted(calls)]))

    def count_tokens(self, messages: Sequence[Message]) -> int:
        chars = 0
        for m in messages:
            chars += len(str(getattr(m, "content", "") or ""))
            for c in getattr(m, "tool_calls", ()) or ():
                chars += len(str(getattr(c, "name", ""))) + len(
                    str(getattr(c, "arguments", "")))
        return max(1, chars // 4)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.provider.embed(list(texts))
