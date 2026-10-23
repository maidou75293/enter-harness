"""action_mode="port"：工具清单与执行走 ActionPort。"""
import unittest

from enter_harness import (Agent, AgentConfig, FakeModel, InMemoryStore,
                           ModelResponse, RunRequest, Scope, ToolCall)
from enter_harness.tools import ToolRegistry
from enter_harness.errors import ConfigError
from enter_harness.harness import ToolActionPort


class AskPolicy:
    def check(self, ctx, action):
        return {"decision": "ask", "reason": "需要审批", "approval_id": "ap_1"}


class DenyPolicy:
    def check(self, ctx, action):
        return {"decision": "deny", "reason": "无权调用"}


def _agent(tools, responses=None, *, action_port=None, mode="port",
           policy=None, handler=None):
    calls = {"tools": []}

    def on_model(messages, tool_schemas, budget):
        calls["tools"].append([_tool_name(t) for t in tool_schemas])
        calls["messages"] = messages
        if handler is not None:
            return handler(messages, tool_schemas, budget)
        return responses.pop(0)

    config = AgentConfig(
        scope=Scope("t1", "a1", "u1", ("assistant",)),
        model=FakeModel(handler=on_model),
        store=InMemoryStore(),
        tools=tools,
        action_mode=mode,
        action_port=action_port,
    )
    return Agent(config), calls


def _tool_name(schema):
    return ((schema.get("function") or {}).get("name")
            or schema.get("name"))


def _call_tool(name, args=None, call_id="c1"):
    return ModelResponse(content="", tool_calls=(
        ToolCall(call_id, name, args or {}),))


class ActionModeTests(unittest.TestCase):
    def test_port_mode_executes_tool(self):
        seen = []
        tools = ToolRegistry()
        tools.register("echo", lambda text="": seen.append(text) or text, "回显")
        agent, calls = _agent(
            tools,
            [ModelResponse(content="", tool_calls=(
                ToolCall("c1", "echo", {"text": "hi"}),)),
             ModelResponse(content="done")],
            action_port=ToolActionPort(tools))
        result = agent.run(RunRequest(objective="echo hi"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "done")
        self.assertEqual(seen, ["hi"])
        self.assertIn("echo", calls["tools"][0])

    def test_port_mode_hides_tool_by_tenant(self):
        seen = []
        tools = ToolRegistry()
        tools.register("secret", lambda: seen.append(1) or "x",
                       "受限工具", tenants=("t2",))
        tools.register("echo", lambda text="": text, "回显")
        port = ToolActionPort(tools)
        agent, calls = _agent(
            tools,
            [ModelResponse(content="", tool_calls=(
                ToolCall("c1", "secret", {}),)),
             ModelResponse(content="done")],
            action_port=port)
        result = agent.run(RunRequest(objective="call secret"))
        self.assertEqual(calls["tools"][0], ["echo"])   # 清单里看不到
        self.assertEqual(seen, [])                      # 也执行不了
        self.assertEqual(result.status, "ok")

    def test_policy_ask_blocks_run(self):
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": "deployed", "部署")
        port = ToolActionPort(tools, policy=AskPolicy())
        agent, _ = _agent(
            tools,
            [ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),))],
            action_port=port)
        result = agent.run(RunRequest(objective="deploy prod"))
        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(result.pending)
        self.assertEqual(result.pending.kind, "approval")
        self.assertEqual(result.pending.data.get("approval_id"), "ap_1")

    def test_policy_deny_returns_denied_observation(self):
        seen = []
        tools = ToolRegistry()
        tools.register("deploy", lambda env="": seen.append(env) or "ok", "部署")
        port = ToolActionPort(tools, policy=DenyPolicy())
        agent, calls = _agent(
            tools,
            [ModelResponse(content="", tool_calls=(
                ToolCall("c1", "deploy", {"env": "prod"}),)),
             ModelResponse(content="denied")],
            action_port=port)
        result = agent.run(RunRequest(objective="deploy prod"))
        self.assertEqual(seen, [])
        observations = [m.content for m in calls["messages"]
                        if getattr(m, "role", "") == "tool"]
        self.assertTrue(any("无权调用" in c for c in observations), observations)
        self.assertEqual(result.answer, "denied")

    def test_invalid_config(self):
        tools = ToolRegistry()
        with self.assertRaises(ConfigError):
            _agent(tools, [], mode="port", action_port=None)
        with self.assertRaises(ConfigError):
            _agent(tools, [], mode="nope")


if __name__ == "__main__":
    unittest.main()
