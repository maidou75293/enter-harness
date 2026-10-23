"""同步工具注册表（v1.1）。

与旧版异步 ToolRegistry 并存；冻结 API 走这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .domain import RunContext, ToolResult
from .errors import ToolError


@dataclass
class FunctionTool:
    name: str
    handler: Callable
    description: str = ""
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    parallel_safe: bool = False
    danger: bool = False
    with_context: bool = False

    def execute(self, ctx: RunContext, args: dict) -> ToolResult:
        try:
            if self.with_context:
                out = self.handler(ctx, **(args or {}))
            else:
                out = self.handler(**(args or {}))
            if isinstance(out, ToolResult):
                return out
            return ToolResult(call_id="", name=self.name, ok=True, content=str(out))
        except ToolError as e:
            return ToolResult(call_id="", name=self.name, ok=False, error=str(e))
        except Exception as e:  # noqa: BLE001 —— 工具失败留给模型/宿主
            return ToolResult(call_id="", name=self.name, ok=False,
                              error=f"{type(e).__name__}: {e}")


class ToolRegistry:
    """同步、线程内使用的工具表；ReAct/Graph 共用同一实例。"""

    def __init__(self) -> None:
        self._tools: dict[str, FunctionTool] = {}

    def register(self, name: str, handler: Callable, description: str = "",
                 parameters: dict | None = None, parallel_safe: bool = False,
                 danger: bool = False, with_context: bool = False) -> FunctionTool:
        tool = FunctionTool(name=name, handler=handler, description=description,
                            parameters=parameters or {"type": "object", "properties": {}},
                            parallel_safe=parallel_safe, danger=danger,
                            with_context=with_context)
        self._tools[name] = tool
        return tool

    def register_tool(self, tool: FunctionTool) -> FunctionTool:
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> FunctionTool | None:
        return self._tools.get(name)

    def list_tools(self, whitelist: list[str] | None = None) -> list[FunctionTool]:
        allow = set(whitelist) if whitelist else None
        return [t for n, t in self._tools.items() if allow is None or n in allow]

    def openai_tools(self, whitelist: list[str] | None = None) -> list[dict]:
        out = []
        for t in self.list_tools(whitelist):
            out.append({"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters,
            }})
        return out

    def execute(self, name: str, args: dict, ctx: RunContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(call_id="", name=name, ok=False,
                              error=f"未知工具：{name}")
        return tool.execute(ctx, args or {})

    def names(self) -> list[str]:
        return list(self._tools)
