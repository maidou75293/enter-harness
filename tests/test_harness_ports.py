"""内核 10 端口契约 + 默认实现用例。"""
import sqlite3
import time
import unittest

from enter_harness.context import (MemoryRecord, SQLiteMemoryStore, WriteResult,
                                assemble, Block, BlockMeta)
from enter_harness.errors import ContextOverflow
from enter_harness.harness import (ActionResult, DefaultIngestPort, MemoryPort,
                                PortSet, RuleVerifyPort, SQLiteAuditPort,
                                SQLiteStatePort, StoreMemoryPort, ToolActionPort)


# ---------------------------------------------------------------- fakes


class FakeIdentity:
    def resolve(self, req=None):
        return {"tenant_id": "t1", "user_id": "u1"}


class FakeContext:
    def list_blocks(self, scope="", scope_id=""):
        return []

    def get_block(self, block_id, version=0):
        return None


class FakeMemory:
    def query(self, req=None):
        return []

    def upsert(self, item):
        return WriteResult(action="append", id="m1")

    def archive(self, ids):
        return 0


class FakeKnowledge:
    def search(self, query, top_k=4, scope=""):
        return []

    def get_chunk(self, chunk_id):
        return None


class FakePolicy:
    def can(self, identity, action, target):
        return True


class FakeIngest:
    def collect(self, req=None):
        return []


class FakeAction:
    def list_tools(self, identity, scope=""):
        return []

    async def call(self, identity, name, args, ctx=None):
        return ActionResult()

    def request_approval(self, identity, call):
        return None

    async def resume(self, ticket_id, decision):
        return ActionResult()


class FakeState:
    def load(self, thread_id):
        return None

    def save(self, thread_id, state):
        return None

    def checkpoint(self, thread_id, node, state=None):
        return "ck1"

    def resume(self, thread_id):
        return None

    def list_checkpoints(self, thread_id):
        return []


class FakeVerify:
    def check(self, result, criteria=None, ctx=None):
        return None


class FakeAudit:
    def emit(self, event):
        return event

    def query(self, filter=None):
        return []


class FakeRegistry:
    def __init__(self):
        self.executed = []

    def list_tools(self):
        return [{"name": "write", "tags": ["action"]}]

    async def execute(self, name, args, ctx):
        self.executed.append((name, args, ctx))
        if ctx.get("approved_override"):
            return {"ok": True, "result": {"done": args}}
        return {"ok": False, "needs_approval": True, "error": "需要审批"}


# ---------------------------------------------------------------- ports


class PortContractTests(unittest.TestCase):
    def test_01_ten_ports(self):
        p = PortSet(identity=FakeIdentity(), context=FakeContext(),
                    memory=FakeMemory(), knowledge=FakeKnowledge(),
                    policy=FakePolicy(), ingest=FakeIngest(),
                    action=FakeAction(), state=FakeState(),
                    verify=FakeVerify(), audit=FakeAudit())
        self.assertEqual(p.missing(), [])
        self.assertEqual(p.invalid(), [])
        self.assertTrue(p.check())

    def test_02_missing_is_visible(self):
        p = PortSet()
        self.assertEqual(len(p.missing()), 10)
        with self.assertRaises(TypeError):
            p.check()

    def test_02b_defaults_satisfy_protocols(self):
        store = SQLiteMemoryStore()
        p = PortSet(identity=FakeIdentity(), context=FakeContext(),
                    memory=StoreMemoryPort(store), knowledge=FakeKnowledge(),
                    policy=FakePolicy(), ingest=DefaultIngestPort(),
                    action=ToolActionPort(FakeRegistry()), state=SQLiteStatePort(),
                    verify=RuleVerifyPort(), audit=SQLiteAuditPort())
        self.assertEqual(p.invalid(), [])
        store.close()


# ---------------------------------------------------------------- ingest


class IngestTests(unittest.TestCase):
    def test_03_normalizes_sources(self):
        port = DefaultIngestPort(default_scope="project", default_scope_id="p1")
        out = port.collect({"items": [
            {"role": "user", "content": "你好"},
            {"tool": "query", "result": {"rows": 3}},
            {"event": "tick"},
            {"role": "user", "content": "   "},
        ]})
        self.assertEqual([x.source for x in out], ["user", "tool", "event"])
        self.assertEqual(out[0].kind_hint, "episodic")
        self.assertIn("rows", out[1].content)
        self.assertEqual(out[0].scope_id, "p1")

    def test_04_single_message(self):
        out = DefaultIngestPort().collect({"role": "assistant", "content": "好"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source, "agent")


# ---------------------------------------------------------------- audit


class AuditTests(unittest.TestCase):
    def test_05_hash_chain_and_query(self):
        sink = SQLiteAuditPort()
        for i in range(3):
            sink.emit({"tenant_id": "t1", "actor": "u1", "action": f"a{i}",
                       "target": "x", "decision": "ok"})
        self.assertEqual(sink.verify_chain()["ok"], True)
        self.assertEqual(len(sink.query({"tenant_id": "t1"})), 3)
        self.assertEqual(len(sink.query({"action": "a1"})), 1)

    def test_06_tamper_detected(self):
        sink = SQLiteAuditPort()
        sink.emit({"tenant_id": "t1", "actor": "u1", "action": "a",
                   "target": "x", "decision": "ok"})
        sink.emit({"tenant_id": "t1", "actor": "u1", "action": "b",
                   "target": "x", "decision": "ok"})
        sink.conn.execute("UPDATE audit SET decision='denied' WHERE action='a'")
        sink.conn.commit()
        self.assertEqual(sink.verify_chain()["ok"], False)

    def test_07_prune_keeps_chain_valid(self):
        sink = SQLiteAuditPort()
        for i, at in enumerate((100.0, 200.0, 300.0)):
            sink.emit({"tenant_id": "t1", "actor": "u1", "action": f"a{i}",
                       "target": "x", "decision": "ok", "at": at})
        self.assertEqual(sink.prune(before=150.0), 1)
        result = sink.verify_chain()
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 2)


# ---------------------------------------------------------------- state


class StateTests(unittest.TestCase):
    def test_07_save_checkpoint_resume(self):
        st = SQLiteStatePort()
        self.assertIsNone(st.load("th1"))
        st.save("th1", {"step": 1})
        self.assertEqual(st.load("th1"), {"step": 1})
        c1 = st.checkpoint("th1", "plan", {"step": 1})
        c2 = st.checkpoint("th1", "act", {"step": 2})
        self.assertEqual(st.resume("th1"), {"step": 2})
        rows = st.list_checkpoints("th1")
        self.assertEqual([r["node"] for r in rows], ["plan", "act"])
        self.assertEqual(rows[1]["parent_id"], c1)
        self.assertNotIn("state", rows[0])
        self.assertTrue(c2)

    def test_07b_prune_states_and_checkpoints(self):
        st = SQLiteStatePort()
        st.save("th1", {"step": 1})
        st.checkpoint("th1", "plan", {"step": 1})
        self.assertEqual(st.prune(before=0), 0)
        self.assertEqual(st.prune(before=time.time() + 1), 2)
        self.assertIsNone(st.load("th1"))
        self.assertEqual(st.list_checkpoints("th1"), [])

    def test_07c_prune_keeps_fresh_rows(self):
        st = SQLiteStatePort()
        st.save("th1", {"step": 1})
        self.assertEqual(st.prune(retention_days=30), 0)
        self.assertIsNotNone(st.load("th1"))


# ---------------------------------------------------------------- action


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_08_approval_roundtrip(self):
        reg = FakeRegistry()
        port = ToolActionPort(reg)
        ident = {"tenant_id": "t1", "user_id": "u1"}

        first = await port.call(ident, "write", {"v": 1})
        self.assertEqual(first.status, "needs_approval")
        self.assertTrue(first.approval_id)

        ticket = port.request_approval(ident, first)
        self.assertEqual(ticket.status, "pending")
        self.assertEqual(ticket.tool, "write")

        done = await port.resume(ticket.id, "approve")
        self.assertTrue(done.ok)
        self.assertEqual(done.result, {"done": {"v": 1}})
        self.assertTrue(reg.executed[-1][2].get("approved_override"))
        self.assertEqual(port.queue.get(ticket.id).status, "approved")

    async def test_09_reject_is_closed(self):
        reg = FakeRegistry()
        port = ToolActionPort(reg)
        ident = {"tenant_id": "t1", "user_id": "u1"}
        first = await port.call(ident, "write", {"v": 1})
        ticket = port.request_approval(ident, first)
        done = await port.resume(ticket.id, "reject")
        self.assertFalse(done.ok)
        self.assertEqual(done.status, "denied")
        self.assertEqual(port.queue.get(ticket.id).status, "rejected")

    async def test_10_unknown_ticket_is_closed(self):
        port = ToolActionPort(FakeRegistry())
        done = await port.resume("nope", "approve")
        self.assertEqual(done.status, "denied")

    async def test_11_duplicate_resume_does_not_re_execute(self):
        reg = FakeRegistry()
        port = ToolActionPort(reg)
        ident = {"tenant_id": "t1", "user_id": "u1"}
        first = await port.call(ident, "write", {"v": 1})
        ticket = port.request_approval(ident, first)
        self.assertTrue((await port.resume(ticket.id, "approve")).ok)
        second = await port.resume(ticket.id, "approve")
        self.assertEqual(second.status, "error")
        self.assertEqual(len(reg.executed), 2)  # 首次 call + 一次恢复执行


# ---------------------------------------------------------------- verify


class VerifyTests(unittest.TestCase):
    def test_11_rules(self):
        port = RuleVerifyPort()
        self.assertFalse(port.check("").passed)
        self.assertFalse(port.check("请手动执行安装").passed)
        self.assertTrue(port.check("已完成").passed)
        self.assertFalse(port.check({"answer": "ok"}, {"required": ["x"]}).passed)
        self.assertFalse(port.check({"answer": 1}, {"schema": {"answer": "str"}}).passed)
        self.assertTrue(port.check({"answer": "ok"},
                                   {"required": ["answer"], "min_chars": 2}).passed)


# ---------------------------------------------------------------- write


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteMemoryStore()

    def tearDown(self):
        self.store.close()

    def _rec(self, **kw):
        base = dict(tenant_id="t1", kind="semantic", scope="project",
                    scope_id="p1", key="库", content="库=a", source="user")
        base.update(kw)
        return MemoryRecord(**base)

    def test_12_four_states(self):
        r1 = self.store.upsert(self._rec())
        self.assertIsInstance(r1, WriteResult)
        self.assertEqual(r1.action, "append")
        self.assertEqual(self.store.upsert(self._rec()).action, "noop")
        r3 = self.store.upsert(self._rec(content="库=b"))
        self.assertEqual(r3.action, "supersede")
        self.assertEqual(r3.replaced_id, r1.id)

    def test_13_invalid_is_explicit(self):
        self.assertEqual(self.store.upsert(self._rec(tenant_id="")).action, "invalid")
        self.assertEqual(self.store.upsert(self._rec(kind="working")).action, "invalid")
        self.assertEqual(self.store.upsert(self._rec(key="")).action, "invalid")
        self.assertEqual(self.store.upsert(self._rec(source="")).action, "invalid")

    def test_14_profile_allowed(self):
        r = self.store.upsert(self._rec(kind="profile", key="", content="先给结论"))
        self.assertEqual(r.action, "append")

    def test_14b_adapter_query_contract(self):
        port = StoreMemoryPort(self.store)
        self.assertIsInstance(port, MemoryPort)
        port.upsert(self._rec())
        got = port.query({"tenant_id": "t1", "scope": "project", "scope_id": "p1"})
        self.assertEqual([r.content for r in got], ["库=a"])


# ---------------------------------------------------------------- budget


def blk(bid, kind, text, **kw):
    meta = BlockMeta(id=bid, kind=kind, **kw)
    return Block(meta, text)


class BudgetTests(unittest.TestCase):
    def test_15_drop_whole_not_half(self):
        ctx = type("C", (), {
            "list_blocks": lambda self, scope="", scope_id="": [
                BlockMeta(id="s", kind="semantic", content_hash="h")],
            "get_block": lambda self, bid, version=0: blk("s", "semantic", "事" * 80),
        })()
        out = assemble(context=ctx, max_total_chars=50)
        self.assertEqual(out["blocks"], [])
        self.assertIn("s", out["dropped"])
        self.assertEqual(out["truncated"], [])
        self.assertEqual(out["text"], "")

    def test_16_instruction_never_truncated(self):
        ctx = type("C", (), {
            "list_blocks": lambda self, scope="", scope_id="": [
                BlockMeta(id="r", kind="procedural", content_hash="h")],
            "get_block": lambda self, bid, version=0: blk("r", "procedural", "规" * 80),
        })()
        with self.assertRaises(ContextOverflow):
            assemble(context=ctx, max_total_chars=50)


if __name__ == "__main__":
    unittest.main()
