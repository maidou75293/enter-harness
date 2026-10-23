"""Session：会话级串行封装（非线程安全）。"""
from __future__ import annotations

from .domain import CompressionResult, Message, RunRequest, RunResult
from .errors import ConfigError


class Session:
    def __init__(self, agent, session_id: str):
        self.agent = agent
        self.session_id = str(session_id or "")
        self._history: list[Message] = []
        self._closed = False

    @property
    def tenant_id(self) -> str:
        return self.agent.scope.tenant_id

    @property
    def agent_id(self) -> str:
        """会话必须绑定智能体：运行时会话继承父 Agent 的 Scope。"""
        return self.agent.scope.agent_id

    def send(self, text: str, **opts) -> RunResult:
        if self._closed:
            raise ConfigError("Session 已关闭")
        if not str(text or "").strip():
            raise ConfigError("send 文本不能为空")
        history = [{"role": m.role, "content": m.content,
                    "tool_calls": [{"id": c.id, "name": c.name,
                                    "arguments": c.arguments} for c in m.tool_calls],
                    "tool_call_id": m.tool_call_id}
                   for m in self._history]
        req = RunRequest(
            objective=str(text),
            acceptance=tuple(opts.get("acceptance") or ()),
            executor=str(opts.get("executor") or ""),
            budget=opts.get("budget"),
            metadata={"history": history, "session_id": self.session_id,
                      **dict(opts.get("metadata") or {})},
            idempotency_key=str(opts.get("idempotency_key") or ""),
            graph=opts.get("graph"),
            cancel_token=opts.get("cancel_token"),
        )
        result = self.agent.run(req)
        self._history.append(Message("user", str(text)))
        if result.answer is not None:
            self._history.append(Message("assistant", str(result.answer)))
        return result

    def compress(self) -> CompressionResult:
        if self._closed:
            raise ConfigError("Session 已关闭")
        res = self.agent.compressor.compress(self._history)
        if res.compressed:
            self._history = self.agent.compressor.apply(self._history, res)
        return res

    def history(self) -> list[Message]:
        return list(self._history)

    def close(self) -> None:
        self._closed = True
