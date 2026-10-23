# modules: enter_harness.api enter_harness.domain enter_harness.ports
"""阶段 2：RunContext / RunState / Agent 构造与 executor 选择。"""
import unittest

from enter_harness.adapters.memory import FakeModel, InMemoryStore
from enter_harness.api import Agent, AgentConfig
from enter_harness.domain import (
    BudgetProfile, GraphNode, GraphSpec, RunRequest, Scope,
)
from enter_harness.errors import ConfigError, ScopeError


def _agent(executor="auto", graph=None):
    return Agent(AgentConfig(
        scope=Scope("t1", "u1", ["arch"]),
        model=FakeModel(),
        store=InMemoryStore(),
        executor=executor,
    ))


class AgentConstructionTests(unittest.TestCase):
    def test_missing_scope_or_ports(self):
        with self.assertRaises(ScopeError):
            Agent(AgentConfig(scope=None, model=FakeModel(), store=InMemoryStore()))
        with self.assertRaises(ConfigError):
            Agent(AgentConfig(scope=Scope("t1"), model=None, store=InMemoryStore()))
        with self.assertRaises(ConfigError):
            Agent(AgentConfig(scope=Scope("t1"), model=FakeModel(), store=None))
        with self.assertRaises(ConfigError):
            Agent(AgentConfig(scope=Scope("t1"), model=FakeModel(),
                              store=InMemoryStore(), executor="nope"))

    def test_context_is_frozen_and_has_scope(self):
        a = _agent()
        req = RunRequest(objective="做事", acceptance=["有结果"])
        ctx = a._build_context(req)
        self.assertEqual(ctx.scope.tenant_id, "t1")
        self.assertEqual(ctx.executor, "react")
        self.assertEqual(ctx.acceptance, ["有结果"])
        with self.assertRaises(Exception):
            ctx.objective = "changed"  # frozen dataclass

    def test_executor_auto_selection(self):
        self.assertEqual(_agent("auto")._resolve_executor(RunRequest("x")), "react")
        graph = GraphSpec(nodes=(GraphNode("a", "tool", tool="calc"),
                                  GraphNode("b", "llm", prompt="汇总")),
                          final_node="b")
        self.assertEqual(_agent("auto")._resolve_executor(
            RunRequest("x", graph=graph)), "graph")
        reactive = GraphSpec(nodes=(GraphNode("a", "react", prompt="探索"),),
                             final_node="a")
        self.assertEqual(_agent("auto")._resolve_executor(
            RunRequest("x", graph=reactive)), "hybrid")
        self.assertEqual(_agent("graph")._resolve_executor(
            RunRequest("x")), "graph")

    def test_state_history_and_budget(self):
        a = _agent()
        req = RunRequest(objective="继续", metadata={"history": [
            {"role": "user", "content": "上一轮"},
            {"role": "assistant", "content": "上次回答"},
        ]}, budget=BudgetProfile(max_steps=3))
        ctx = a._build_context(req)
        st = a._build_state(ctx, req)
        self.assertEqual([m.content for m in st.messages], ["上一轮", "上次回答", "继续"])
        self.assertEqual(ctx.budget.max_steps, 3)
        self.assertEqual(st.status, "running")


if __name__ == "__main__":
    unittest.main()
