# modules: agent_core.executors agent_core.api
"""阶段 5：react / graph / hybrid 三种执行器与 auto 选择。"""
import unittest

from agent_core.adapters.memory import FakeModel, InMemoryStore
from agent_core.api import Agent, AgentConfig
from agent_core.domain import (
    GraphNode, GraphSpec, ModelResponse, RunRequest, Scope,
)
from agent_core.tooling import ToolRegistry


def _agent(mode, responses):
    tools = ToolRegistry()
    tools.register("echo", lambda text="": text, "回显")
    return Agent(AgentConfig(scope=Scope("t1", "u1", ["arch"]),
                             model=FakeModel(responses), store=InMemoryStore(),
                             tools=tools, executor=mode))


class ExecutorTests(unittest.TestCase):
    def test_graph_tool_then_llm(self):
        graph = GraphSpec(nodes=(
            GraphNode("a", "tool", tool="echo", args={"text": "hello"}),
            GraphNode("b", "llm", prompt="汇总", depends_on=("a",)),
        ), final_node="b")
        a = _agent("graph", [ModelResponse(content="最终答复")])
        r = a.run(RunRequest(objective="流程", graph=graph))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.answer, "最终答复")
        self.assertEqual([row.node_id for row in r.state.ledger], ["a", "b"])

    def test_hybrid_llm_node_runs_react(self):
        graph = GraphSpec(nodes=(
            GraphNode("a", "react", prompt="探索并回答"),
        ), final_node="a")
        a = _agent("hybrid", [ModelResponse(content="hybrid 答案")])
        r = a.run(RunRequest(objective="混合", graph=graph))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.answer, "hybrid 答案")

    def test_auto_selects_graph_and_hybrid(self):
        graph = GraphSpec(nodes=(
            GraphNode("a", "tool", tool="echo", args={"text": "x"}),
            GraphNode("b", "llm", prompt="汇总", depends_on=("a",)),
        ), final_node="b")
        a = _agent("auto", [ModelResponse(content="graph 答案")])
        self.assertEqual(a._resolve_executor(RunRequest("x", graph=graph)), "graph")
        hybrid = GraphSpec(nodes=(GraphNode("a", "react", prompt="探索"),),
                           final_node="a")
        self.assertEqual(a._resolve_executor(RunRequest("x", graph=hybrid)), "hybrid")
        self.assertEqual(a._resolve_executor(RunRequest("x")), "react")

    def test_graph_cycle_rejected(self):
        graph = GraphSpec(nodes=(
            GraphNode("a", "tool", tool="echo", depends_on=("b",)),
            GraphNode("b", "tool", tool="echo", depends_on=("a",)),
        ), final_node="a")
        a = _agent("graph", [])
        r = a.run(RunRequest(objective="环", graph=graph))
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error.code, "schema_error")


if __name__ == "__main__":
    unittest.main()
