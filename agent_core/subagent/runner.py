"""SubagentRunner：隔离子代理。

设计：
- 全新消息列表（system + task），不继承父上下文；父进程只拿回结果。
- 工具白名单：从父注册表 `subset()` 出一个受限视图；父工具对象不被修改。
- 嵌套深度：contextvar 计数，超过 max_depth 抛 SubagentDepthExceeded。
- 复用 AgentLoop，因此同样具备 max_steps / timeout / 中断 / trace。
"""
from __future__ import annotations

from ..errors import SubagentDepthExceeded
from ..hooks.manager import HookManager
from ..loop.loop import AgentLoop
from ..runtime import current_depth
from ..tracing.tracer import Tracer


class SubagentRunner:
    def __init__(self, provider, tools, *, max_depth: int = 2, hooks: HookManager | None = None,
                 tracer: Tracer | None = None, system_prompt: str = "",
                 max_steps: int = 6, timeout: float | None = 120):
        self.provider = provider
        self.tools = tools
        self.max_depth = int(max_depth)
        self.hooks = hooks or HookManager()
        self.tracer = tracer or Tracer()
        self.system_prompt = system_prompt or (
            "你是子代理：只完成分配给你的子任务。上下文与工具都是隔离的；"
            "完成后只返回简洁结果，不要寒暄。")
        self.max_steps = max_steps
        self.timeout = timeout

    async def run(self, task: str, *, tools: list | None = None,
                  system_prompt: str | None = None, max_steps: int | None = None,
                  timeout: float | None = None, messages: list | None = None) -> dict:
        depth = current_depth.get()
        if depth >= self.max_depth:
            raise SubagentDepthExceeded(
                f"子代理嵌套深度超限：{depth} >= {self.max_depth}")
        registry = self.tools.subset(tools) if tools is not None else self.tools
        loop = AgentLoop(
            self.provider, registry,
            system_prompt=system_prompt if system_prompt is not None else self.system_prompt,
            max_steps=max_steps if max_steps is not None else self.max_steps,
            timeout=timeout if timeout is not None else self.timeout,
            hooks=self.hooks, tracer=self.tracer)
        token = current_depth.set(depth + 1)
        try:
            r = await loop.run(task, messages=messages)
        finally:
            current_depth.reset(token)
        return {"ok": r.ok, "answer": r.answer, "steps": r.steps,
                "stopped_reason": r.stopped_reason, "interrupted": r.interrupted,
                "error": r.error, "trace": r.trace}
