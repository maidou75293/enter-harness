"""Markdown 本地存储：记忆 / 知识 / 工厂。"""
import tempfile
import unittest
from pathlib import Path

from enter_harness.context import MemoryRecord, content_hash
from enter_harness.harness import (MarkdownKnowledgePort, MarkdownMemoryPort,
                                build_local_ports)
from enter_harness.harness.markdown_io import parse


def _rec(**kw):
    base = dict(tenant_id="t1", kind="semantic", scope="project",
                scope_id="p1", key="项目库", content="库=a", source="user")
    base.update(kw)
    return MemoryRecord(**base)


class MarkdownMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.port = MarkdownMemoryPort(self.root / "memory")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_query(self):
        r = self.port.upsert(_rec())
        self.assertEqual(r.action, "append")
        rows = self.port.query({"tenant_id": "t1", "scope": "project",
                                "scope_id": "p1"})
        self.assertEqual([x.content for x in rows], ["库=a"])
        files = list((self.root / "memory").rglob("*.md"))
        self.assertEqual(len(files), 1)
        meta, body = parse(files[0].read_text(encoding="utf-8"))
        self.assertEqual(meta["tenant_id"], "t1")
        self.assertEqual(body.strip(), "库=a")

    def test_noop_accumulates_provenance(self):
        self.port.upsert(_rec())
        r = self.port.upsert(_rec(source="agent"))
        self.assertEqual(r.action, "noop")
        rows = self.port.query({"tenant_id": "t1"})
        self.assertEqual(len(rows), 1)
        self.assertIn("agent", [p["source"] for p in rows[0].provenance])

    def test_supersede(self):
        first = self.port.upsert(_rec())
        r = self.port.upsert(_rec(content="库=b"))
        self.assertEqual(r.action, "supersede")
        self.assertEqual(r.replaced_id, first.id)
        self.assertEqual([x.content for x in self.port.query({"tenant_id": "t1"})],
                         ["库=b"])
        old = self.port.query({"tenant_id": "t1", "status": "superseded"})
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0].superseded_by, r.id)

    def test_invalid(self):
        self.assertEqual(self.port.upsert(_rec(tenant_id="")).action, "invalid")
        self.assertEqual(self.port.upsert(_rec(source="")).action, "invalid")

    def test_archive(self):
        r = self.port.upsert(_rec())
        self.assertEqual(self.port.archive([r.id], reason="user_retracted"), 1)
        self.assertEqual(self.port.query({"tenant_id": "t1"}), [])
        rows = self.port.query({"tenant_id": "t1", "status": "archived"})
        self.assertEqual(rows[0].status_reason, "user_retracted")

    def test_tenant_isolation(self):
        self.port.upsert(_rec(tenant_id="t1"))
        self.port.upsert(_rec(tenant_id="t2", content="t2"))
        self.assertEqual([x.content for x in self.port.query({"tenant_id": "t1"})],
                         ["库=a"])
        self.assertEqual([x.content for x in self.port.query({"tenant_id": "t2"})],
                         ["t2"])

    def test_human_edit_is_visible(self):
        r = self.port.upsert(_rec())
        path = next((self.root / "memory").rglob("*.md"))
        meta, body = parse(path.read_text(encoding="utf-8"))
        path.write_text(
            path.read_text(encoding="utf-8").replace(body, "库=人手改的"),
            encoding="utf-8")
        rows = self.port.query({"tenant_id": "t1"})
        self.assertEqual(rows[0].content, "库=人手改的")
        self.assertEqual(rows[0].content_hash, content_hash("库=人手改的"))
        self.assertEqual(r.id, rows[0].id)


class MarkdownKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = MarkdownKnowledgePort(Path(self.tmp.name) / "knowledge")

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, **kw):
        data = dict(tenant_id="t1", scope="project", scope_id="p1",
                    title="报销制度", source="handbook.md",
                    content="报销需要审批，金额超过 5000 需要总监审批")
        data.update(kw)
        return self.port.add(data)

    def test_pending_not_searchable(self):
        cid = self._add()
        self.assertEqual(self.port.search("报销", tenant_id="t1"), [])
        self.assertIsNotNone(self.port.get_chunk(cid))

    def test_approve_then_search(self):
        cid = self._add()
        self.assertTrue(self.port.approve(cid, reviewer="admin"))
        rows = self.port.search("报销 审批", tenant_id="t1")
        self.assertEqual(rows[0]["id"], cid)
        self.assertEqual(rows[0]["review_status"], "approved")
        self.assertTrue(rows[0]["source"])

    def test_archive(self):
        cid = self._add()
        self.port.approve(cid)
        self.assertTrue(self.port.archive(cid))
        self.assertIsNone(self.port.get_chunk(cid))
        self.assertEqual(self.port.search("报销", tenant_id="t1"), [])

    def test_source_required(self):
        with self.assertRaises(ValueError):
            self.port.add({"tenant_id": "t1", "content": "x", "source": ""})

    def test_tenant_isolation(self):
        self._add(tenant_id="t1")
        self._add(tenant_id="t2", content="t2 制度")
        for cid in (self.port.search("报销", tenant_id="t1")):
            self.assertEqual(cid["tenant_id"], "t1")
        self.assertEqual(self.port.search("报销", tenant_id="t3"), [])

    def test_human_edit_demotes_to_pending(self):
        cid = self._add()
        self.port.approve(cid)
        path = next((Path(self.tmp.name) / "knowledge").rglob("*.md"))
        path.write_text(path.read_text(encoding="utf-8") + "\n新增未审核内容",
                        encoding="utf-8")
        self.assertEqual(self.port.search("报销", tenant_id="t1"), [])
        self.assertEqual(self.port.get_chunk(cid)["review_status"], "pending")


class FactoryTests(unittest.TestCase):
    def test_sqlite_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            ports = build_local_ports(tmp, "sqlite")
            self.assertEqual(ports.backend, "sqlite")
            self.assertTrue((Path(tmp) / "memory.db").exists())
            self.assertTrue((Path(tmp) / "knowledge.db").exists())
            ports.close()

    def test_markdown_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            ports = build_local_ports(tmp, "markdown")
            self.assertIsInstance(ports.memory, MarkdownMemoryPort)
            self.assertIsInstance(ports.knowledge, MarkdownKnowledgePort)
            self.assertTrue((Path(tmp) / "memory").is_dir())
            self.assertTrue((Path(tmp) / "knowledge").is_dir())
            ports.close()

    def test_unknown_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_local_ports(tmp, "mysql")


if __name__ == "__main__":
    unittest.main()
