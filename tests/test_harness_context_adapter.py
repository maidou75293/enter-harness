"""MemoryContextPort（记忆 → 上下文块）的用例。

全部基于真实 SQLiteMemoryStore + StoreMemoryPort，验证
「写一条记忆 → 下一轮拼装可见」的闭环，而不是只测 mock。
"""
import unittest

from agent_core.context import (Block, BlockMeta, ContextPort, MemoryRecord,
                                SQLiteMemoryStore, assemble)
from agent_core.harness.context import MemoryContextPort
from agent_core.harness.memory import StoreMemoryPort


def rec(**kw):
    """一条合法的 semantic 记录；用 kw 覆盖字段构造场景。"""
    base = dict(tenant_id="t1", kind="semantic", scope="project",
                scope_id="p1", key="库", content="库=a", source="user")
    base.update(kw)
    return MemoryRecord(**base)


def ids(metas):
    return [m.id for m in metas]


class _LeakyMemory:
    """反模式：无视查询条件返回全部记录，用来验证适配器自己再挡一层。"""

    def __init__(self, store, record_ids):
        self.store = store
        self._record_ids = list(record_ids)

    def query(self, req=None):
        return [self.store.get(i) for i in self._record_ids]


class _DupMemory:
    """故意重复返回同一条记录，验证 list_blocks 按 id 去重。"""

    def __init__(self, store, record_id):
        self.store = store
        self._record_id = record_id

    def query(self, req=None):
        row = self.store.get(self._record_id)
        return [row, row]


class _BareMemory:
    """没有 store 属性的最小 MemoryPort。"""

    def query(self, req=None):
        return []


class MemoryContextPortTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteMemoryStore()
        self.memory = StoreMemoryPort(self.store)

    def tearDown(self):
        self.store.close()

    def test_01_metadata_and_body(self):
        """写入 semantic → list_blocks 有元数据，get_block 有正文（summary 优先）。"""
        record = rec(importance=0.75, summary="摘要：库在 dev",
                     content="正文：库在 dev")
        result = self.memory.upsert(record)
        self.assertEqual(result.action, "append")

        port = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertIsInstance(port, ContextPort)

        metas = port.list_blocks()
        self.assertEqual(len(metas), 1)
        meta = metas[0]
        self.assertIsInstance(meta, BlockMeta)
        self.assertEqual(meta.id, record.id)
        self.assertEqual(meta.kind, "semantic")
        self.assertEqual(meta.scope, "project")
        self.assertEqual(meta.scope_id, "p1")
        self.assertEqual(meta.tenant_id, "t1")
        self.assertEqual(meta.priority, 250)          # (1 - 0.75) * 1000
        self.assertEqual(meta.version, 1)
        self.assertEqual(meta.source, "user")
        self.assertEqual(meta.owner, "memory")
        self.assertTrue(meta.cacheable)
        self.assertEqual(meta.content_hash, record.content_hash)

        block = port.get_block(record.id)
        self.assertIsInstance(block, Block)
        self.assertEqual(block.text, "摘要：库在 dev")
        self.assertEqual(block.meta.id, record.id)
        self.assertEqual(block.meta.priority, 250)

    def test_02_write_then_assemble_roundtrip(self):
        """闭环：本轮写入的事实，下一轮 assemble 就能看到。"""
        record = rec(content="本轮写入的事实")
        self.memory.upsert(record)

        port = MemoryContextPort(self.memory, tenant_id="t1")
        out = assemble(context=port, identity={"tenant_id": "t1"})

        self.assertIn("本轮写入的事实", out["text"])
        self.assertEqual([b["id"] for b in out["blocks"]], [record.id])
        self.assertEqual(out["used_chars"], len("本轮写入的事实"))
        self.assertEqual(out["dropped"], [])

    def test_03_supersede_only_new_content_visible(self):
        """同 key 写新内容：旧记录 superseded，拼装只见新内容。"""
        old = rec(key="库", content="旧库=dev")
        self.memory.upsert(old)
        new = rec(key="库", content="新库=prod")
        written = self.memory.upsert(new)
        self.assertEqual(written.action, "supersede")
        self.assertEqual(written.replaced_id, old.id)

        port = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertEqual(ids(port.list_blocks()), [new.id])

        out = assemble(context=port)
        self.assertIn("新库=prod", out["text"])
        self.assertNotIn("旧库=dev", out["text"])
        self.assertIsNone(port.get_block(old.id))     # 非 active 不给正文

    def test_04_archived_records_hidden(self):
        """归档后列表、正文、拼装三处都看不到。"""
        record = rec(content="已归档的事实")
        self.memory.upsert(record)
        self.assertEqual(self.memory.archive([record.id]), 1)

        port = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertEqual(port.list_blocks(), [])
        self.assertIsNone(port.get_block(record.id))
        self.assertEqual(assemble(context=port)["text"], "")

    def test_05_tenant_isolation(self):
        """t2 的记录对 t1 不可见；memory 漏过滤时适配器也要挡住。"""
        r1 = rec(tenant_id="t1", key="k1", content="t1 的事实")
        r2 = rec(tenant_id="t2", key="k2", content="t2 的事实")
        self.memory.upsert(r1)
        self.memory.upsert(r2)

        port_t1 = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertEqual(ids(port_t1.list_blocks()), [r1.id])
        out = assemble(context=port_t1, identity={"tenant_id": "t1"})
        self.assertIn("t1 的事实", out["text"])
        self.assertNotIn("t2 的事实", out["text"])
        self.assertIsNone(port_t1.get_block(r2.id))

        port_t2 = MemoryContextPort(self.memory, tenant_id="t2")
        self.assertEqual(ids(port_t2.list_blocks()), [r2.id])
        self.assertIsNone(port_t2.get_block(r1.id))

        # 防御层：即使 MemoryPort 无视 tenant 条件，适配器仍按 tenant 过滤
        leaky = MemoryContextPort(_LeakyMemory(self.store, [r1.id, r2.id]),
                                  tenant_id="t1")
        self.assertEqual(ids(leaky.list_blocks()), [r1.id])

    def test_06_scope_and_scope_id_filter(self):
        """显式 scope 优先，缺省回退构造参数里的默认 scope。"""
        p1 = rec(key="k1", scope="project", scope_id="p1", content="p1 事实")
        p2 = rec(key="k2", scope="project", scope_id="p2", content="p2 事实")
        u1 = rec(kind="profile", key="k3", scope="user", scope_id="u1",
                 content="用户偏好")
        for r in (p1, p2, u1):
            self.memory.upsert(r)

        port = MemoryContextPort(self.memory, tenant_id="t1",
                                 default_scope="project",
                                 default_scope_id="p1")
        self.assertEqual(ids(port.list_blocks()), [p1.id])
        self.assertEqual(ids(port.list_blocks(scope="project", scope_id="p2")),
                         [p2.id])
        self.assertEqual(ids(port.list_blocks(scope="user", scope_id="u1")),
                         [u1.id])

        # 不设默认值 = 不按 scope 过滤
        all_port = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertEqual(len(all_port.list_blocks()), 3)

    def test_07_empty_store_is_safe(self):
        """空库：列表为空、正文为 None、assemble 返回空且不报错。"""
        port = MemoryContextPort(self.memory, tenant_id="t1")
        self.assertEqual(port.list_blocks(), [])
        self.assertIsNone(port.get_block("missing"))

        out = assemble(context=port)
        self.assertEqual(out["text"], "")
        self.assertEqual(out["blocks"], [])
        self.assertEqual(out["used_chars"], 0)

        bare = MemoryContextPort(_BareMemory(), tenant_id="t1")
        self.assertIsNone(bare.get_block("missing"))

    def test_08_priority_formula_and_text_fallback(self):
        """priority=(1-importance)*1000；无 summary 时正文取 content。"""
        low = rec(key="low", content="低重要", importance=0.75)
        high = rec(key="high", content="高重要", importance=0.25)
        half = rec(key="half", content="中重要", importance=0.5)
        for r in (low, high, half):
            self.memory.upsert(r)

        port = MemoryContextPort(self.memory, tenant_id="t1")
        prio = {m.id: m.priority for m in port.list_blocks()}
        self.assertEqual(prio, {low.id: 250, high.id: 750, half.id: 500})

        block = port.get_block(low.id)
        self.assertEqual(block.text, "低重要")
        # version 仅作协议兼容：存储只有当前版本
        self.assertEqual(port.get_block(low.id, version=99).text, "低重要")

    def test_09_duplicate_ids_are_deduped(self):
        """同一 id 被重复返回时，list_blocks 只保留一条。"""
        record = rec(content="只应出现一次")
        self.memory.upsert(record)
        port = MemoryContextPort(_DupMemory(self.store, record.id),
                                 tenant_id="t1")
        self.assertEqual(ids(port.list_blocks()), [record.id])


if __name__ == "__main__":
    unittest.main()
