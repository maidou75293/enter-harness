# modules: agent_core.compression agent_core.api agent_core.executors
"""阶段 4：压缩、RunHooks 调用点、memory_delta 事务落库。"""
import unittest

from agent_core.adapters.memory import FakeModel, InMemoryStore
from agent_core.api import Agent, AgentConfig
from agent_core.domain import (
    BudgetProfile, Message, ModelResponse, RunRequest, Scope, ToolCall,
)
from agent_core.tooling import ToolRegistry


class CountingHooks:
    def __init__(self):
        self.calls = []

    def before_model_call(self, ctx, state):
        self.calls.append("before_model")

    def after_tool_batch(self, ctx, state):
        self.calls.append("after_tools")

    def on_context_pressure(self, ctx, state):
        self.calls.append("pressure")

    def before_finish(self, ctx, state):
        self.calls.append("finish")

    def on_cancel(self, ctx, state):
        self.calls.append("cancel")


def _tools():
    t = ToolRegistry()
    t.register("echo", lambda text="": text, "回显")
    return t


class CompressionWriteTests(unittest.TestCase):
    def test_tool_results_commit(self):
        store = InMemoryStore()
        a = Agent(AgentConfig(
            scope=Scope("t1", "u1", ["arch"]),
            model=FakeModel([ModelResponse(content="", tool_calls=(
                ToolCall("c1", "echo", {"text": "hello"}),)), ModelResponse(content="ok")]),
            store=store, tools=_tools()))
        r = a.run(RunRequest(objective="回显"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(store._items), 1)          # 工具结果已提交
        self.assertGreaterEqual(len(store._ledger), 1)

    def test_hooks_called(self):
        hooks = CountingHooks()
        a = Agent(AgentConfig(
            scope=Scope("t1"), model=FakeModel([ModelResponse(content="ok")]),
            store=InMemoryStore(), hooks=hooks))
        r = a.run(RunRequest(objective="hi"))
        self.assertEqual(r.status, "ok")
        self.assertIn("before_model", hooks.calls)
        self.assertIn("finish", hooks.calls)

    def test_context_pressure_compresses_and_commits(self):
        store = InMemoryStore()
        long_history = [{"role": "user", "content": "历史" * 80} for _ in range(6)]
        # 1 工具调用 -> 2 压缩摘要 -> 3 最终答复
        a = Agent(AgentConfig(
            scope=Scope("t1", "u1", ["arch"]),
            model=FakeModel([
                ModelResponse(content="", tool_calls=(ToolCall("c1", "echo", {"text": "x"}),)),
                ModelResponse(content="压缩摘要：目标是回显"),
                ModelResponse(content="最终答复"),
            ]),
            store=store, tools=_tools()))
        r = a.run(RunRequest(
            objective="长历史任务", budget=BudgetProfile(max_steps=4, max_tokens=20),
            metadata={"history": long_history}))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.answer, "最终答复")
        self.assertEqual(r.state.summary, "压缩摘要：目标是回显")
        self.assertTrue(any(m.role == "system" and "历史摘要" in m.content
                            for m in r.state.messages))
        self.assertGreaterEqual(len(store._items), 2)   # 工具结果 + 摘要
        self.assertTrue(any("压缩摘要" in it.content for it in store._items.values()))

    def test_session_compress(self):
        a = Agent(AgentConfig(scope=Scope("t1"), model=FakeModel([
            ModelResponse(content="会话摘要")]), store=InMemoryStore()))
        s = a.session("s1")
        s._history = [Message("user", "u" * 50) for _ in range(6)]
        res = s.compress()
        self.assertTrue(res.compressed)
        self.assertEqual(res.summary, "会话摘要")
        self.assertEqual(len(s.history()), 5)           # 1 摘要 + 4 保留
        self.assertIn("历史摘要", s.history()[0].content)

    def test_cancel_hook(self):
        class CancelHooks(CountingHooks):
            pass
        hooks = CancelHooks()

        class CancelModel:
            def complete(self, messages, tools, budget):
                from agent_core.domain import CancelToken
                raise RuntimeError("cancel")
            def stream(self, *a, **kw):
                yield from ()
            def count_tokens(self, messages):
                return 1
            def embed(self, texts):
                return [[0.0] for _ in texts]

        a = Agent(AgentConfig(scope=Scope("t1"), model=CancelModel(),
                              store=InMemoryStore(), hooks=hooks))
        r = a.run(RunRequest(objective="cancel"))
        self.assertEqual(r.status, "failed")
        self.assertIn("before_model", hooks.calls)


if __name__ == "__main__":
    unittest.main()
