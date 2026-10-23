"""PostgreSQL 端口用例；没有 HARNESS_PG_DSN 时跳过。"""
import os
import unittest
import uuid

from agent_core.context import MemoryRecord
from agent_core.harness import (PostgresAuditPort, PostgresKnowledgePort,
                                PostgresMemoryPort, PostgresStatePort)

DSN = os.environ.get("HARNESS_PG_DSN", "")


@unittest.skipUnless(DSN, "未设置 HARNESS_PG_DSN，跳过 PostgreSQL 用例")
class PostgresPortTests(unittest.TestCase):
    def setUp(self):
        self.tenant = "pg_test_" + uuid.uuid4().hex[:8]
        self.memory = PostgresMemoryPort(DSN)
        self.knowledge = PostgresKnowledgePort(DSN)
        self.state = PostgresStatePort(DSN)
        self.audit = PostgresAuditPort(DSN)

    def tearDown(self):
        for port in (self.memory, self.knowledge, self.state, self.audit):
            port.close()

    def test_memory_four_states(self):
        base = dict(tenant_id=self.tenant, kind="semantic", scope="project",
                    scope_id="p1", key="库", content="库=a", source="user")
        self.assertEqual(self.memory.upsert(MemoryRecord(**base)).action, "append")
        self.assertEqual(self.memory.upsert(MemoryRecord(**base)).action, "noop")
        r = self.memory.upsert(MemoryRecord(**{**base, "content": "库=b"}))
        self.assertEqual(r.action, "supersede")
        self.assertEqual([x.content for x in self.memory.query(
            {"tenant_id": self.tenant})], ["库=b"])

    def test_knowledge_review_flow(self):
        cid = self.knowledge.add({
            "tenant_id": self.tenant, "scope": "project", "scope_id": "p1",
            "title": "报销制度", "source": "handbook.md",
            "content": "报销需要审批"})
        self.assertEqual(self.knowledge.search("报销", tenant_id=self.tenant), [])
        self.assertTrue(self.knowledge.approve(cid))
        self.assertEqual(self.knowledge.search("报销", tenant_id=self.tenant)[0]["id"],
                         cid)
        self.assertTrue(self.knowledge.archive(cid))
        self.assertIsNone(self.knowledge.get_chunk(cid))

    def test_state_checkpoint(self):
        thread = "th_" + uuid.uuid4().hex[:8]
        self.state.save(thread, {"step": 1})
        self.state.checkpoint(thread, "plan", {"step": 2})
        self.state.checkpoint(thread, "act", {"step": 3})
        self.assertEqual(self.state.resume(thread), {"step": 3})
        self.assertEqual([x["node"] for x in self.state.list_checkpoints(thread)],
                         ["plan", "act"])

    def test_audit_chain(self):
        self.audit.emit({"tenant_id": self.tenant, "actor": "u1",
                         "action": "a", "target": "x", "decision": "ok"})
        self.audit.emit({"tenant_id": self.tenant, "actor": "u1",
                         "action": "b", "target": "x", "decision": "ok"})
        self.assertTrue(self.audit.verify_chain()["ok"])
        self.assertEqual(len(self.audit.query({"tenant_id": self.tenant})), 2)


if __name__ == "__main__":
    unittest.main()
