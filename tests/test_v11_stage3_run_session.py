# modules: enter_harness.api enter_harness.executors enter_harness.session_api
"""阶段 3：run / stream / start / session 打通（FakeModel + InMemoryStore）。"""
import unittest

from enter_harness.adapters.memory import FakeModel, InMemoryEventSink, InMemoryStore
from enter_harness.api import Agent, AgentConfig
from enter_harness.domain import (
    BudgetProfile, CancelToken, Message, ModelResponse, RunRequest, Scope,
    ToolCall,
)
from enter_harness.tooling import ToolRegistry


def _agent(responses, store=None, sink=None):
    tools = ToolRegistry()
    tools.register("calc", lambda expr="": str(eval(expr)), "计算",
                   {"type": "object", "properties": {"expr": {"type": "string"}}})
    return Agent(AgentConfig(
        scope=Scope("t1", "u1", ["arch"]),
        model=FakeModel(responses),
        store=store or InMemoryStore(),
        events=sink,
        tools=tools,
    ))


class RunTests(unittest.TestCase):
    def test_react_tool_call_then_answer(self):
        store = InMemoryStore()
        a = _agent([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc", {"expr": "1+1"}),)),
            ModelResponse(content="结果是 2"),
        ], store=store)
        r = a.run(RunRequest(objective="算 1+1"))
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.answer, "结果是 2")
        self.assertEqual(r.state.step, 2)
        self.assertTrue(any(m.role == "tool" and "2" in m.content for m in r.state.messages))
        self.assertEqual(len(store._ledger), 1)
        self.assertEqual(store._ledger[0].status, "ok")

    def test_max_steps_reports_budget_error(self):
        a = _agent([
            ModelResponse(content="", tool_calls=(ToolCall(f"c{i}", "calc", {"expr": "1+1"}),))
            for i in range(4)
        ])
        r = a.run(RunRequest(objective="长任务",
                             budget=BudgetProfile(max_steps=2)))
        self.assertEqual(r.status, "failed")
        self.assertIsNotNone(r.error)
        self.assertEqual(r.error.code, "budget_exceeded")

    def test_cancel_token_stops_before_model_call(self):
        tok = CancelToken()
        tok.cancel()
        a = _agent([ModelResponse(content="不应调用")])
        r = a.run(RunRequest(objective="x", cancel_token=tok))
        self.assertEqual(r.status, "cancelled")
        self.assertEqual(r.error.code, "cancelled")

    def test_stream_and_event_sink(self):
        sink = InMemoryEventSink()
        a = _agent([ModelResponse(content="ok")], sink=sink)
        events = list(a.stream(RunRequest(objective="hi")))
        self.assertIn("start", [e.type for e in events])
        self.assertIn("done", [e.type for e in events])
        # 非流式 run 走构造注入的 event sink
        a.run(RunRequest(objective="hi"))
        self.assertIn("done", [e.type for e in sink.events])

    def test_start_handle_and_cancel(self):
        a = _agent([ModelResponse(content="ok")])
        h = a.start(RunRequest(objective="hi"))
        r = h.wait(timeout=5)
        self.assertEqual(r.status, "ok")
        self.assertIn(h.status(), ("ok", "running"))

    def test_idempotency_key(self):
        a = _agent([ModelResponse(content="first")])
        req = RunRequest(objective="hi", idempotency_key="same")
        r1 = a.run(req)
        r2 = a.run(req)
        self.assertIs(r1, r2)


class SessionTests(unittest.TestCase):
    def test_session_history(self):
        a = _agent([ModelResponse(content="答 1"), ModelResponse(content="答 2")])
        s = a.session("s1")
        r1 = s.send("问 1")
        r2 = s.send("问 2")
        self.assertEqual(r1.answer, "答 1")
        self.assertEqual(r2.answer, "答 2")
        self.assertEqual([m.content for m in s.history()],
                         ["问 1", "答 1", "问 2", "答 2"])
        self.assertFalse(s.compress().compressed)
        s.close()
        with self.assertRaises(Exception):
            s.send("again")


if __name__ == "__main__":
    unittest.main()
