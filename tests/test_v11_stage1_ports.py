# modules: agent_core.domain agent_core.ports agent_core.adapters.memory
"""阶段 1：Scope、端口类型、InMemoryStore 事务/隔离、FakeModel。"""
import unittest

from agent_core.adapters.memory import FakeModel, InMemoryStore
from agent_core.domain import (
    BudgetProfile, ItemDraft, LedgerRow, Message, ModelResponse, Scope,
    ToolCall, Topic,
)
from agent_core.errors import SchemaError


class ScopeTests(unittest.TestCase):
    def test_scope_requires_tenant_and_normalizes_roles(self):
        with self.assertRaises(ValueError):
            Scope("")
        s = Scope("t1", "u1", ["r2", "r1", "r1"])
        self.assertEqual(s.tenant_id, "t1")
        self.assertEqual(s.role_ids, ("r1", "r2"))

    def test_scope_is_hashable(self):
        self.assertEqual(len({Scope("t1", "u1", ("r1",)), Scope("t1", "u1", ("r1",))}), 1)


class MessageTests(unittest.TestCase):
    def test_as_dict_serializes_tool_arguments(self):
        """OpenAI 兼容接口要求 tool_calls.function.arguments 是字符串。"""
        m = Message("assistant", "", tool_calls=(
            ToolCall("c1", "calc", {"expr": "1+1"}),))
        d = m.as_dict()
        self.assertEqual(d["tool_calls"][0]["function"]["arguments"],
                         '{"expr": "1+1"}')
        # 已经是字符串时不重复编码
        m2 = Message("assistant", "", tool_calls=(
            ToolCall("c2", "calc", '{"x": 1}'),))
        self.assertEqual(m2.as_dict()["tool_calls"][0]["function"]["arguments"],
                         '{"x": 1}')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.t1 = Scope("t1", "u1", ("arch",))
        self.t2 = Scope("t2", "u2", ("arch",))

    def test_transaction_commit_and_rollback(self):
        with self.store.transaction() as tx:
            iid = tx.put_item(self.t1, ItemDraft(content="端口 9000"))
        self.assertIsNotNone(self.store.get_item(self.t1, iid))
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as tx:
                tx.put_item(self.t1, ItemDraft(content="不应提交"))
                raise RuntimeError("boom")
        self.assertEqual(len(self.store.search(self.t1, "", {}, 10)), 1)

    def test_tenant_isolation(self):
        with self.store.transaction() as tx:
            tx.put_item(self.t1, ItemDraft(content="t1 私有"))
            tx.put_item(self.t2, ItemDraft(content="t2 私有"))
        hits = self.store.search(self.t1, "", {}, 10)
        self.assertEqual(len(hits), 1)
        self.assertIn("t1", hits[0].summary)
        self.assertEqual(len(self.store.search(self.t2, "", {}, 10)), 1)
        self.assertNotEqual(hits[0].item_id,
                            self.store.search(self.t2, "", {}, 10)[0].item_id)

    def test_role_and_user_filter(self):
        with self.store.transaction() as tx:
            tx.put_item(Scope("t1", "u1", ("arch",)), ItemDraft(content="角色经验"))
            tx.put_item(Scope("t1", "u1", ("ops",)), ItemDraft(content="运维经验"))
            tx.put_item(Scope("t1", "", ()), ItemDraft(content="租户知识"))
        self.assertEqual(len(self.store.search(Scope("t1", "u1", ("arch",)), "", {}, 10)), 2)
        # u1 的个人条目对 u2 不可见；u2 只看到租户知识
        self.assertEqual(len(self.store.search(Scope("t1", "u2", ("ops",)), "", {}, 10)), 1)

    def test_schema_and_ledger(self):
        self.assertEqual(self.store.schema_version(), "1")
        with self.store.transaction() as tx:
            tx.put_ledger(self.t1, LedgerRow(run_id="r1", node_id="n1", digest="ok"))
            tx.put_deps("r1", "n0", "n1")
            tx.put_topic(self.t1, Topic(topic_id="tp1", title="端口", scope=self.t1))
        with self.assertRaises(SchemaError):
            self.store.migrate("0", "1")


class FakeModelTests(unittest.TestCase):
    def test_scripted_complete_and_tokens(self):
        m = FakeModel([ModelResponse(content="hello")])
        r = m.complete([Message("user", "hi")], [], BudgetProfile())
        self.assertEqual(r.content, "hello")
        self.assertGreater(m.count_tokens([Message("user", "hello")]), 0)
        self.assertEqual(len(m.embed(["a", "b"])), 2)


if __name__ == "__main__":
    unittest.main()
