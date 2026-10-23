# modules: enter_harness
"""阶段 6：冻结接口契约测试 + 最小示例。"""
import unittest

import enter_harness as ac
from enter_harness import (
    Agent, AgentConfig, FakeModel, GraphNode, GraphSpec, InMemoryStore,
    ModelResponse, RunRequest, Scope, ToolCall, ToolRegistry,
)
from enter_harness.errors import ModelError, ScopeError


def _tools():
    t = ToolRegistry()
    t.register("echo", lambda text="": text, "回显")
    return t


class ContractTests(unittest.TestCase):
    def test_public_api_exists(self):
        for name in ("Agent", "AgentConfig", "Scope", "RunContext", "RunState",
                     "RunRequest", "RunResult", "Executor", "RunHooks",
                     "ModelPort", "StorePort", "PolicyPort", "ToolRegistry",
                     "Session", "RunHandle", "Event", "BudgetProfile",
                     "ScopeError", "ModelError", "StoreError", "CancelledError"):
            self.assertTrue(hasattr(ac, name), name)

    def test_missing_scope(self):
        with self.assertRaises(ScopeError):
            Agent(AgentConfig(scope=None, model=FakeModel(),
                              store=InMemoryStore()))

    def test_three_modes_same_answer(self):
        def react():
            return Agent(AgentConfig(
                scope=Scope("t1", "u1", ["a"]),
                model=FakeModel([
                    ModelResponse(content="", tool_calls=(
                        ToolCall("c1", "echo", {"text": "x"}),)),
                    ModelResponse(content="同一答案")]),
                store=InMemoryStore(), tools=_tools(), executor="react"))

        graph_spec = GraphSpec(nodes=(
            GraphNode("a", "tool", tool="echo", args={"text": "x"}),
            GraphNode("b", "llm", prompt="汇总", depends_on=("a",)),
        ), final_node="b")
        hybrid_spec = GraphSpec(nodes=(
            GraphNode("a", "react", prompt="探索并回答"),
        ), final_node="a")

        r1 = react().run(RunRequest("x"))
        r2 = Agent(AgentConfig(
            scope=Scope("t1", "u1", ["a"]),
            model=FakeModel([ModelResponse(content="同一答案")]),
            store=InMemoryStore(), tools=_tools(), executor="graph")
        ).run(RunRequest("x", graph=graph_spec))
        r3 = Agent(AgentConfig(
            scope=Scope("t1", "u1", ["a"]),
            model=FakeModel([ModelResponse(content="同一答案")]),
            store=InMemoryStore(), tools=_tools(), executor="hybrid")
        ).run(RunRequest("x", graph=hybrid_spec))
        self.assertEqual([r1.status, r2.status, r3.status], ["ok", "ok", "ok"])
        self.assertEqual([r1.answer, r2.answer, r3.answer],
                         ["同一答案", "同一答案", "同一答案"])

    def test_cross_tenant_isolation(self):
        store = InMemoryStore()
        for tid in ("t1", "t2"):
            a = Agent(AgentConfig(
                scope=Scope(tid, "u", ["r"]),
                model=FakeModel([
                    ModelResponse(content="", tool_calls=(
                        ToolCall("c1", "echo", {"text": tid}),)),
                    ModelResponse(content=tid)]),
                store=store, tools=_tools()))
            a.run(RunRequest("写一条"))
        self.assertEqual(len(store.search(Scope("t1", "u", ["r"]), "", {}, 10)), 1)
        self.assertEqual(len(store.search(Scope("t2", "u", ["r"]), "", {}, 10)), 1)

    def test_model_error_code(self):
        class BadModel:
            def complete(self, *a, **kw):
                raise RuntimeError("boom")
            def stream(self, *a, **kw):
                yield from ()
            def count_tokens(self, messages):
                return 1
            def embed(self, texts):
                return [[0.0] for _ in texts]

        a = Agent(AgentConfig(scope=Scope("t1"), model=BadModel(),
                              store=InMemoryStore()))
        r = a.run(RunRequest("失败"))
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error.code, ModelError.code)

    def test_inmemory_ports_replaceable(self):
        a = Agent(AgentConfig(scope=Scope("t1"), model=FakeModel(
            [ModelResponse(content="ok")]), store=InMemoryStore()))
        self.assertEqual(a.run(RunRequest("hi")).answer, "ok")

    def test_retrieval_fills_run_state(self):
        from enter_harness import ItemDraft
        store = InMemoryStore()
        with store.transaction() as tx:
            tx.put_item(Scope("t1", "u", ["r"]), ItemDraft(content="端口是 9000"))
        a = Agent(AgentConfig(scope=Scope("t1", "u", ["r"]),
                              model=FakeModel([ModelResponse(content="ok")]),
                              store=store))
        r = a.run(RunRequest("端口"))
        self.assertTrue(r.state.retrieved)
        self.assertIn("9000", r.state.retrieved[0].summary + r.state.retrieved[0].title)


class ExampleTests(unittest.TestCase):
    def test_minimal_host_runs(self):
        from examples.minimal_host import main
        result = main()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "工具返回 hello")


if __name__ == "__main__":
    unittest.main()
