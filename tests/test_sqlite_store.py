"""默认 SQLite 存储：内核写/读/归档的用例。"""
import unittest

from agent_core.context import MemoryRecord, SQLiteMemoryStore


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteMemoryStore()

    def tearDown(self):
        self.store.close()

    def _rec(self, **kw):
        base = dict(tenant_id="t1", kind="semantic", scope="project",
                    scope_id="p1", key="项目库", content="库=a",
                    source="user")
        base.update(kw)
        return MemoryRecord(**base)

    def test_append_and_query(self):
        r = self.store.upsert(self._rec())
        self.assertEqual(r["action"], "append")
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual([x.content for x in rows], ["库=a"])

    def test_idempotent_same_content(self):
        self.store.upsert(self._rec())
        r = self.store.upsert(self._rec(source="agent"))
        self.assertEqual(r["action"], "noop")
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual(len(rows), 1)
        self.assertIn("agent", [p["source"] for p in rows[0].provenance])

    def test_supersede_same_key(self):
        self.store.upsert(self._rec())
        r = self.store.upsert(self._rec(content="库=b"))
        self.assertEqual(r["action"], "supersede")
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual([x.content for x in rows], ["库=b"])
        old = self.store.query("t1", "project", "p1", status="superseded")
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0].superseded_by, r["id"])
        self.assertEqual(old[0].status_reason, "superseded")

    def test_same_key_different_kind_coexist(self):
        """同名 key 的语义事实与程序性规则互不覆盖。"""
        self.store.upsert(self._rec(kind="semantic", content="库=X"))
        self.store.upsert(self._rec(kind="procedural", content="改库要审批"))
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual(len(rows), 2)

    def test_condition_separates(self):
        self.store.upsert(self._rec(condition={"env": "DEV"}, content="dev"))
        self.store.upsert(self._rec(condition={"env": "PROD"}, content="prod"))
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual(sorted(x.content for x in rows), ["dev", "prod"])

    def test_agent_id_separates(self):
        self.store.upsert(self._rec(agent_id="agent:a", content="A"))
        self.store.upsert(self._rec(agent_id="agent:b", content="B"))
        rows = self.store.query("t1", "project", "p1")
        self.assertEqual(sorted(x.content for x in rows), ["A", "B"])

    def test_tenant_isolation(self):
        self.store.upsert(self._rec(tenant_id="t1"))
        self.store.upsert(self._rec(tenant_id="t2", content="t2"))
        self.assertEqual([x.content for x in self.store.query("t1")], ["库=a"])
        self.assertEqual([x.content for x in self.store.query("t2")], ["t2"])

    def test_expired_hidden(self):
        self.store.upsert(self._rec(expires_at=1.0))
        self.assertEqual(self.store.query("t1", "project", "p1"), [])
        self.assertEqual(len(self.store.query("t1", "project", "p1",
                                              include_expired=True)), 1)

    def test_archive_reason(self):
        r = self.store.upsert(self._rec())
        n = self.store.archive([r["id"]], reason="user_retracted")
        self.assertEqual(n, 1)
        rows = self.store.query("t1", "project", "p1", status="archived")
        self.assertEqual(rows[0].status_reason, "user_retracted")


if __name__ == "__main__":
    unittest.main()
