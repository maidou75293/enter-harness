# modules: enter_harness.agents enter_harness.adapters.memory enter_harness.domain enter_harness.ports
"""v1.1 多智能体：Agent/Session 管理 API 与租户/智能体隔离。"""
import unittest

from enter_harness.adapters.memory import InMemoryStore
from enter_harness.agents import DEFAULT_AGENT_ID, AgentService
from enter_harness.domain import ItemDraft, Scope
from enter_harness.errors import ConfigError, ConflictError, NotFoundError


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.svc = AgentService(self.store)

    def test_agent_crud(self):
        a1 = self.svc.create_agent(
            "t1", "助手 A", role="ops",
            config={"model_tier": "high", "executor": "graph",
                    "tool_whitelist": ["calc"], "prompt_profile": "default"})
        a2 = self.svc.create_agent("t1", "助手 B")
        self.assertEqual(a1.config.model_tier, "high")
        self.assertEqual(a1.config.executor, "graph")
        self.assertEqual(a1.config.tool_whitelist, ("calc",))
        self.assertEqual(a1.role, "ops")

        self.assertEqual({x.agent_id for x in self.svc.list_agents("t1")},
                         {a1.agent_id, a2.agent_id})
        self.assertIsNone(self.svc.get_agent("t2", a1.agent_id))

        upd = self.svc.update_agent("t1", a1.agent_id, name="助手 A2",
                                    status="disabled")
        self.assertEqual(upd.name, "助手 A2")
        self.assertEqual(upd.status, "disabled")
        self.assertEqual(upd.created_at, a1.created_at)

        self.assertTrue(self.svc.delete_agent("t1", a2.agent_id))
        self.assertIsNone(self.svc.get_agent("t1", a2.agent_id))
        self.assertFalse(self.svc.delete_agent("t1", a2.agent_id))

        with self.assertRaises(ConflictError):
            self.svc.create_agent("t1", "重复", agent_id=a1.agent_id)
        with self.assertRaises(NotFoundError):
            self.svc.update_agent("t1", "ag_missing", name="x")
        with self.assertRaises(ConfigError):
            self.svc.create_agent("t1", "坏配置", config={"nope": 1})
        with self.assertRaises(ConfigError):
            self.svc.create_agent("t1", "  ")
        with self.assertRaises(ConfigError):
            self.svc.create_agent("", "无租户")

    def test_sessions_bound_to_agent_and_isolated(self):
        a1 = self.svc.create_agent("t1", "助手 A")
        a2 = self.svc.create_agent("t1", "助手 B")
        s1 = self.svc.create_session("t1", a1.agent_id, user_id="u1", title="一")
        s2 = self.svc.create_session("t1", a1.agent_id, user_id="u2", title="二")
        s3 = self.svc.create_session("t1", a2.agent_id, user_id="u1", title="三")

        self.assertEqual(s1.agent_id, a1.agent_id)
        self.assertEqual({s.session_id for s in
                          self.svc.list_sessions("t1", a1.agent_id)},
                         {s1.session_id, s2.session_id})
        self.assertEqual([s.session_id for s in
                          self.svc.list_sessions("t1", a2.agent_id)],
                         [s3.session_id])
        # 跨租户读不到
        self.assertIsNone(self.svc.get_session("t2", s1.session_id))
        # 会话必须绑定存在的智能体
        with self.assertRaises(NotFoundError):
            self.svc.create_session("t1", "ag_missing")

    def test_default_agent_compat(self):
        # 不指定智能体：自动创建默认智能体，旧接口行为不变
        s = self.svc.create_session("t1", user_id="u1")
        self.assertEqual(s.agent_id, DEFAULT_AGENT_ID)
        self.assertIsNotNone(self.svc.get_agent("t1", DEFAULT_AGENT_ID))
        scope = self.svc.scope("t1", user_id="u1")
        self.assertEqual(scope.agent_id, DEFAULT_AGENT_ID)
        # 旧的位置参数写法仍然按 (tenant, user, roles) 解析
        legacy = Scope("t1", "u1", ("arch",))
        self.assertEqual((legacy.tenant_id, legacy.agent_id,
                          legacy.user_id, legacy.role_ids),
                         ("t1", DEFAULT_AGENT_ID, "u1", ("arch",)))

    def test_agent_memory_isolation(self):
        a1 = self.svc.create_agent("t1", "助手 A")
        a2 = self.svc.create_agent("t1", "助手 B")
        sc1 = self.svc.scope("t1", a1.agent_id, user_id="u1")
        sc2 = self.svc.scope("t1", a2.agent_id, user_id="u1")
        other_user_same_agent = Scope(tenant_id="t1", agent_id=a1.agent_id,
                                      user_id="u2")

        with self.store.transaction() as tx:
            tx.put_item(sc1, ItemDraft(content="A 的智能体记忆"))
            tx.put_item(sc1, ItemDraft(content="租户共享知识", owner_type="tenant",
                                       owner_id="t1"))

        # 同一智能体的不同会话/用户共享 agent 级记忆
        self.assertEqual(len(self.store.search(sc1, "", {}, 10)), 2)
        self.assertEqual(len(self.store.search(other_user_same_agent, "", {}, 10)), 2)
        # 不同智能体之间：只看到显式的租户级记忆
        hits = self.store.search(sc2, "", {}, 10)
        self.assertEqual(len(hits), 1)
        self.assertIn("租户共享知识", hits[0].summary or hits[0].title)
        # 跨租户：一条都看不到
        self.assertEqual(len(self.store.search(
            Scope("t2", "u1", ("arch",)), "", {}, 10)), 0)

    def test_runtime_session_and_memory_bound_to_agent(self):
        from enter_harness.adapters.memory import FakeModel
        from enter_harness.api import Agent, AgentConfig
        from enter_harness.domain import ModelResponse, ToolCall
        from enter_harness.tooling import ToolRegistry

        a1 = self.svc.create_agent("t1", "助手 A")
        a2 = self.svc.create_agent("t1", "助手 B")
        tools = ToolRegistry()
        tools.register("echo", lambda text="": text, "回显")
        agent = Agent(AgentConfig(
            scope=self.svc.scope("t1", a1.agent_id, user_id="u1"),
            model=FakeModel([
                ModelResponse(content="", tool_calls=(
                    ToolCall("c1", "echo", {"text": "hello"}),)),
                ModelResponse(content="ok"),
            ]),
            store=self.store, tools=tools))
        s = agent.session("s1")
        # 运行时会话继承父 Agent 的 tenant/agent
        self.assertEqual((s.tenant_id, s.agent_id), ("t1", a1.agent_id))
        r = s.send("回显 hello")
        self.assertEqual(r.status, "ok")

        # 工具结果落库为 agent 级记忆：同智能体（换用户）可见，跨智能体不可见
        self.assertEqual(len(self.store.search(
            self.svc.scope("t1", a1.agent_id, user_id="u2"), "hello", {}, 10)), 1)
        self.assertEqual(len(self.store.search(
            self.svc.scope("t1", a2.agent_id, user_id="u1"), "hello", {}, 10)), 0)

    def test_session_touch(self):
        a = self.svc.create_agent("t1", "助手 A")
        s = self.svc.create_session("t1", a.agent_id)
        touched = self.svc.touch_session("t1", s.session_id)
        self.assertGreaterEqual(touched.updated_at, s.updated_at)
        with self.assertRaises(NotFoundError):
            self.svc.touch_session("t1", "s_missing")


if __name__ == "__main__":
    unittest.main()
