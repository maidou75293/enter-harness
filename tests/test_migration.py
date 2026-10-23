"""三套本地后端之间的迁移用例（记忆 + 知识）。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from enter_harness.context import MemoryRecord
from enter_harness.errors import ConfigError
from enter_harness.harness.factory import build_local_ports
from enter_harness.harness.migrate import dump_ports, load_ports, migrate_ports


def _mem(tenant_id: str, key: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        tenant_id=tenant_id, kind="semantic", scope="project",
        scope_id="p1", key=key, content=content, source="user")


def _chunk(tenant_id: str, content: str, *, chunk_id: str = "",
           title: str = "", status: str = "", reviewer: str = "") -> dict:
    data = {
        "tenant_id": tenant_id,
        "scope": "project",
        "scope_id": "p1",
        "title": title or content,
        "source": "doc.md",
        "content": content,
    }
    if chunk_id:
        data["id"] = chunk_id
    if status:
        data["review_status"] = status
    if reviewer:
        data["reviewer"] = reviewer
    return data


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._ports = []

    def tearDown(self):
        for port in reversed(self._ports):
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()

    def _build(self, name: str, backend: str):
        port = build_local_ports(self.tmp / name, backend)
        self._ports.append(port)
        return port

    def _seed(self, ports, tenant_id: str = "t1"):
        """两条记忆 + 两条知识（一条已审、一条待审）。"""
        ports.memory.upsert(_mem(tenant_id, "k1", "记忆一"))
        ports.memory.upsert(_mem(tenant_id, "k2", "记忆二"))
        approved = ports.knowledge.add(_chunk(
            tenant_id, "知识一", chunk_id="kb_one", status="approved",
            reviewer="alice"))
        pending = ports.knowledge.add(_chunk(
            tenant_id, "知识二", chunk_id="kb_two"))
        return approved, pending

    # 1. sqlite -> markdown：记忆与知识导入后查询一致
    def test_sqlite_to_markdown(self):
        src = self._build("src", "sqlite")
        dst = self._build("dst", "markdown")
        approved, pending = self._seed(src)

        report = migrate_ports(src, dst, tenant_id="t1")
        self.assertEqual(report["memory"], 2)
        self.assertEqual(report["knowledge"], 2)
        self.assertEqual(report["failed"], 0, report["errors"])
        self.assertEqual(report["errors"], [])

        memories = dst.memory.query({"tenant_id": "t1"})
        self.assertEqual(len(memories), 2)
        self.assertEqual(
            {r.key: r.content for r in memories},
            {"k1": "记忆一", "k2": "记忆二"})

        chunks = {c["id"]: c for c in dst.knowledge.dump("t1")}
        self.assertEqual(set(chunks), {approved, pending})
        self.assertEqual(chunks[approved]["content"], "知识一")
        self.assertEqual(chunks[approved]["review_status"], "approved")
        self.assertEqual(chunks[approved]["reviewer"], "alice")
        self.assertEqual(chunks[pending]["content"], "知识二")
        self.assertEqual(chunks[pending]["review_status"], "pending")
        # 目标端按审核状态可检索
        hits = dst.knowledge.search("知识一", tenant_id="t1")
        self.assertEqual([h["id"] for h in hits], [approved])

    # 2. markdown -> sqlite 回迁：id / 内容 / 审核状态保持
    def test_markdown_to_sqlite_roundtrip(self):
        md = self._build("md", "markdown")
        approved, pending = self._seed(md)
        mem_ids = {r.id for r in md.memory.query({"tenant_id": "t1"})}

        sqlite_dst = self._build("sqlite_dst", "sqlite")
        report = migrate_ports(md, sqlite_dst, tenant_id="t1")
        self.assertEqual(report["memory"], 2)
        self.assertEqual(report["knowledge"], 2)
        self.assertEqual(report["failed"], 0, report["errors"])

        got_mem = sqlite_dst.memory.query({"tenant_id": "t1"})
        self.assertEqual({r.id for r in got_mem}, mem_ids)
        self.assertEqual(
            {r.key: r.content for r in got_mem},
            {"k1": "记忆一", "k2": "记忆二"})

        chunks = {c["id"]: c for c in sqlite_dst.knowledge.dump("t1")}
        self.assertEqual(set(chunks), {approved, pending})
        self.assertEqual(chunks[approved]["content"], "知识一")
        self.assertEqual(chunks[approved]["review_status"], "approved")
        self.assertEqual(chunks[pending]["review_status"], "pending")
        # 待审块不参与检索，已审块可以
        self.assertEqual(
            [h["id"] for h in sqlite_dst.knowledge.search("知识", tenant_id="t1")],
            [approved])

    # 3. 重复迁移幂等：记忆 active 条数不翻倍，知识按 id 覆盖
    def test_repeated_migration_is_idempotent(self):
        src = self._build("src", "sqlite")
        dst = self._build("dst", "markdown")
        self._seed(src)

        first = migrate_ports(src, dst, tenant_id="t1")
        self.assertEqual(first["memory"], 2)
        self.assertEqual(first["knowledge"], 2)
        active_after_first = dst.memory.query(
            {"tenant_id": "t1", "status": "active"})
        self.assertEqual(len(active_after_first), 2)

        second = migrate_ports(src, dst, tenant_id="t1")
        self.assertEqual(second["memory"], 2)
        self.assertEqual(second["knowledge"], 2)
        self.assertEqual(second["failed"], 0, second["errors"])

        active_after_second = dst.memory.query(
            {"tenant_id": "t1", "status": "active"})
        self.assertEqual(len(active_after_second), 2)
        self.assertEqual(
            {r.id for r in active_after_second},
            {r.id for r in active_after_first})
        self.assertEqual(len(dst.knowledge.dump("t1")), 2)
        # 目标端知识文件没有因重复导入而增加
        files = list((self.tmp / "dst" / "knowledge").rglob("*.md"))
        self.assertEqual(len(files), 2)

    # 4. dump 按 tenant 过滤
    def test_dump_filters_by_tenant(self):
        src = self._build("src", "sqlite")
        src.memory.upsert(_mem("t1", "k1", "t1 记忆一"))
        src.memory.upsert(_mem("t1", "k2", "t1 记忆二"))
        src.memory.upsert(_mem("t2", "k1", "t2 记忆"))
        src.knowledge.add(_chunk("t1", "t1 知识", chunk_id="kb_t1"))
        src.knowledge.add(_chunk("t2", "t2 知识", chunk_id="kb_t2"))

        data = dump_ports(src, "t1")
        self.assertEqual(len(data["memory"]), 2)
        self.assertEqual({row["tenant_id"] for row in data["memory"]}, {"t1"})
        self.assertEqual(len(data["knowledge"]), 1)
        self.assertEqual(data["knowledge"][0]["id"], "kb_t1")
        self.assertEqual(data["knowledge"][0]["tenant_id"], "t1")

        # 空租户只导出空租户的数据
        empty = dump_ports(src, "")
        self.assertEqual(empty, {"memory": [], "knowledge": []})

        # 目标端同样按租户过滤（markdown 也没有 t2 的数据）
        dst = self._build("dst", "markdown")
        load_ports(dst, data)
        self.assertEqual(len(dump_ports(dst, "t1")["memory"]), 2)
        self.assertEqual(dump_ports(dst, "t2"),
                         {"memory": [], "knowledge": []})

    # 5. 端口缺少 dump 能力时给出明确 ConfigError
    def test_missing_dump_capability_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            dump_ports(SimpleNamespace(memory=object(), knowledge=object()))
        self.assertIn("dump", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class MigrationStateTests(unittest.TestCase):
    """审核状态与时间戳必须原样保留（含 rejected）。"""

    def test_rejected_and_timestamps_preserved(self):
        import tempfile
        from enter_harness.harness.factory import build_local_ports
        from enter_harness.harness.migrate import migrate_ports

        with tempfile.TemporaryDirectory() as src_dir, \
                tempfile.TemporaryDirectory() as dst_dir:
            src = build_local_ports(src_dir, backend="sqlite")
            dst = build_local_ports(dst_dir, backend="markdown")
            try:
                chunk_id = src.knowledge.add({
                    "tenant_id": "t1", "scope": "project", "scope_id": "p1",
                    "title": "x", "content": "old", "source": "s",
                    "review_status": "rejected",
                    "created_at": 1000.0, "updated_at": 2000.0,
                })
                migrate_ports(src, dst, tenant_id="t1")

                # 源端同 id 改内容，仍保持 rejected（显式状态优先）
                src.knowledge.add({
                    "id": chunk_id, "tenant_id": "t1", "scope": "project",
                    "scope_id": "p1", "title": "x", "content": "new",
                    "source": "s", "review_status": "rejected",
                    "created_at": 1000.0, "updated_at": 3000.0,
                })
                report = migrate_ports(src, dst, tenant_id="t1")
                self.assertEqual(report["failed"], 0)

                got = dst.knowledge.get_chunk(chunk_id, tenant_id="t1")
                self.assertEqual(got["review_status"], "rejected")
                self.assertEqual(got["content"], "new")
                self.assertEqual(float(got["created_at"]), 1000.0)
                self.assertEqual(float(got["updated_at"]), 3000.0)
            finally:
                src.close()
                dst.close()
