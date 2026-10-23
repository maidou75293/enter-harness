"""审批恢复：沿用同一 run_id，并从挂起状态继续。"""
import unittest

from enter_harness import (Agent, AgentConfig, FakeModel, InMemoryStore,
                           ModelResponse, RunRequest, Scope, ToolCall,
                           ToolRegistry, Decision)


class AskPolicy:
    def check(self, ctx, action):
        return Decision(decision="ask", reason="需要审批", approval_id="ap_1")


class AllowPolicy:
    def check(self, ctx, action):
        return Decision(decision="allow")


def _agent(tools, responses, policy):
    return Agent(AgentConfig(
        scope=Scope("t1", "a1", "u1", ("assistant",)),
        model=FakeModel(responses),
        store=InMemoryStore(),
        tools=tools,
        policy=policy,
    ))


class ResumeSameRunTests(unittest.TestCase):
    def test_approve_continues_same_run(self):
        executed = []
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": executed.append(env) or "ok", "部署")
        responses = [
            ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),)),
            # 恢复后模型重新发起同一调用（审批已通过，直接执行）
            ModelResponse(content="", tool_calls=(
                ToolCall("c2", "deploy", {"env": "prod"}),)),
            ModelResponse(content="done"),
        ]
        agent = _agent(tools, responses, AskPolicy())
        handle = agent.start(RunRequest(objective="deploy prod"))
        first = handle.wait(timeout=10)
        self.assertEqual(first.status, "blocked")
        self.assertEqual(executed, [])

        handle.resume("approve")
        second = handle.wait(timeout=10)
        self.assertEqual(second.status, "ok")
        self.assertEqual(second.answer, "done")
        self.assertEqual(second.run_id, first.run_id)       # 同一个 run
        self.assertEqual(second.trace_id, first.trace_id)
        self.assertEqual(executed, ["prod"])                 # 只执行一次
        self.assertGreater(len(second.state.messages), 0)

    def test_reject_does_not_execute_and_keeps_run(self):
        executed = []
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": executed.append(env) or "ok", "部署")
        responses = [
            ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),)),
            ModelResponse(content="rejected"),
        ]
        agent = _agent(tools, responses, AskPolicy())
        handle = agent.start(RunRequest(objective="deploy prod"))
        first = handle.wait(timeout=10)
        self.assertEqual(first.status, "blocked")

        handle.resume("reject")
        second = handle.wait(timeout=10)
        self.assertEqual(executed, [])
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.status, "ok")


if __name__ == "__main__":
    unittest.main()


class IdempotencyResumeTests(unittest.TestCase):
    def test_resume_ignores_cached_blocked_result(self):
        executed = []
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": executed.append(env) or "ok", "部署")
        responses = [
            ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),)),
            ModelResponse(content="", tool_calls=(
                ToolCall("c2", "deploy", {"env": "prod"}),)),
            ModelResponse(content="done"),
        ]
        agent = _agent(tools, responses, AskPolicy())
        handle = agent.start(RunRequest(objective="deploy prod",
                                        idempotency_key="same-key"))
        first = handle.wait(timeout=10)
        self.assertEqual(first.status, "blocked")
        handle.resume("approve")
        second = handle.wait(timeout=10)
        self.assertEqual(second.status, "ok")
        self.assertEqual(executed, ["prod"])
        self.assertEqual(second.run_id, first.run_id)


class FlakySink:
    def __init__(self):
        self.fail_next = False
        self.events = []

    def emit(self, event):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("sink down")
        self.events.append(event)


class HandleFailureTests(unittest.TestCase):
    def test_failed_resume_does_not_poison_next_attempt(self):
        executed = []
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": executed.append(env) or "ok", "部署")
        responses = [
            ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),)),
            ModelResponse(content="", tool_calls=(
                ToolCall("c2", "deploy", {"env": "prod"}),)),
            ModelResponse(content="done"),
        ]
        sink = FlakySink()
        agent = Agent(AgentConfig(
            scope=Scope("t1", "a1", "u1", ("assistant",)),
            model=FakeModel(responses), store=InMemoryStore(), tools=tools,
            policy=AskPolicy(), events=sink))
        handle = agent.start(RunRequest(objective="deploy prod"))
        self.assertEqual(handle.wait(timeout=10).status, "blocked")

        sink.fail_next = True
        handle.resume("approve")
        with self.assertRaises(RuntimeError):
            handle.wait(timeout=10)
        self.assertEqual(handle.status(), "failed")

        handle.resume("approve")
        second = handle.wait(timeout=10)
        self.assertEqual(second.status, "ok")
        self.assertEqual(executed, ["prod"])
        self.assertEqual(handle.status(), "ok")


class ConcurrentIdempotencyTests(unittest.TestCase):
    def test_same_key_runs_once(self):
        import threading
        import time

        calls = []

        def handler(messages, tools, budget):
            calls.append(1)
            time.sleep(0.05)
            return ModelResponse(content="done")

        agent = Agent(AgentConfig(
            scope=Scope("t1", "a1", "u1", ("assistant",)),
            model=FakeModel(handler=handler), store=InMemoryStore()))
        results = []

        def worker():
            results.append(agent.run(RunRequest(objective="x",
                                                idempotency_key="k1")))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])
