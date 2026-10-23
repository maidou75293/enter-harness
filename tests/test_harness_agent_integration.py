# modules: agent_core.api agent_core.executors agent_core.harness
"""Harness 端口接入 Agent 主循环：审计 / 检查点 / 审批 / 验收。"""
import unittest

from agent_core.adapters.memory import FakeModel, InMemoryStore
from agent_core.api import Agent, AgentConfig
from agent_core.domain import Decision, ModelResponse, RunRequest, Scope, ToolCall
from agent_core.harness import (RuleVerifyPort, SQLiteApprovalQueue,
                                SQLiteAuditPort, SQLiteStatePort)
from agent_core.tooling import ToolRegistry


class AskPolicy:
    def check(self, ctx, action):
        return Decision("ask", "需要审批", "ap_test")


def _agent(model, *, policy=None, tools=None, **kw):
    return Agent(AgentConfig(
        scope=Scope(tenant_id="t1", agent_id="a1", user_id="u1"),
        model=model,
        store=InMemoryStore(),
        policy=policy,
        tools=tools or ToolRegistry(),
        **kw,
    ))


class AuditIntegrationTests(unittest.TestCase):
    def test_run_start_done_are_audited(self):
        sink = SQLiteAuditPort()
        a = _agent(FakeModel([ModelResponse(content="ok")]), audit=sink)
        r = a.run(RunRequest(objective="hello"))
        self.assertEqual(r.status, "ok")
        actions = [e.action for e in sink.query({"tenant_id": "t1"})]
        self.assertIn("run:start", actions)
        self.assertIn("run:done", actions)
        self.assertTrue(sink.verify_chain()["ok"])


class StateIntegrationTests(unittest.TestCase):
    def test_checkpoints_start_and_done(self):
        sp = SQLiteStatePort()
        a = _agent(FakeModel([ModelResponse(content="ok")]), state_port=sp)
        r = a.run(RunRequest(objective="hello"))
        nodes = [c["node"] for c in sp.list_checkpoints(r.run_id)]
        self.assertEqual(nodes, ["start", "done"])
        self.assertEqual(sp.resume(r.run_id)["status"], "ok")


class ApprovalIntegrationTests(unittest.TestCase):
    def _tools(self, seen):
        tools = ToolRegistry()
        tools.register("calc", lambda expr="": (seen.append(expr) or "2"), "计算")
        return tools

    def test_block_then_approve_executes(self):
        seen = []
        queue = SQLiteApprovalQueue()
        model = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="结果是 2"),
        ])
        a = _agent(model, policy=AskPolicy(), tools=self._tools(seen),
                   approval_queue=queue)
        r = a.run(RunRequest(objective="算 1+1"))
        self.assertEqual(r.status, "blocked")
        self.assertIsNotNone(r.pending)
        data = dict(r.pending.data)
        self.assertEqual(data["approval_id"], "ap_test")
        self.assertEqual(seen, [])
        self.assertEqual(queue.get("ap_test").status, "pending")

        model2 = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="结果是 2"),
        ])
        a2 = _agent(model2, policy=AskPolicy(), tools=self._tools(seen),
                    approval_queue=queue)
        r2 = a2.run(RunRequest(
            objective="算 1+1",
            metadata={"approval_decision": {**data, "decision": "approve"}}))
        self.assertEqual(r2.status, "ok")
        self.assertEqual(seen, ["1+1"])
        self.assertEqual(queue.get("ap_test").status, "approved")

    def test_reject_does_not_execute(self):
        seen = []
        queue = SQLiteApprovalQueue()
        model = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="没有执行"),
        ])
        a = _agent(model, policy=AskPolicy(), tools=self._tools(seen),
                   approval_queue=queue)
        r = a.run(RunRequest(objective="算 1+1"))
        self.assertEqual(r.status, "blocked")
        data = dict(r.pending.data)

        model2 = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="没有执行"),
        ])
        a2 = _agent(model2, policy=AskPolicy(), tools=self._tools(seen),
                    approval_queue=queue)
        r2 = a2.run(RunRequest(
            objective="算 1+1",
            metadata={"approval_decision": {**data, "decision": "reject"}}))
        self.assertEqual(seen, [])
        self.assertEqual(queue.get("ap_test").status, "rejected")
        self.assertIn(r2.status, ("ok", "failed"))


class VerifyIntegrationTests(unittest.TestCase):
    def test_criteria_failure_marks_run_failed(self):
        a = _agent(FakeModel([ModelResponse(content="短")]),
                   verify_port=RuleVerifyPort(),
                   verify_criteria={"min_chars": 100})
        r = a.run(RunRequest(objective="写长一点"))
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.error.code, "verification_failed")


if __name__ == "__main__":
    unittest.main()
