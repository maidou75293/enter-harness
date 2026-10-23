"""统一记忆/上下文模型的一致性用例（规范 v1.3 §9）。

只断言块 id 序列 / 预算行为 / trace 字段，不比对正文措辞，
这样三个产品（enter-agent、dataworks、labelstudio）可以共用同一套用例。
"""
import time
import unittest

from enter_harness.context import (Block, BlockCache, BlockMeta, MemoryRecord,
                                assemble, content_hash, derive_authority,
                                select_records)


class FakeContext:
    """最小 ContextPort：元数据便宜、正文按需取。"""

    def __init__(self, blocks):
        self.blocks = {b.meta.id: b for b in blocks}
        self.fetches = []

    def list_blocks(self, scope="", scope_id=""):
        return [b.meta for b in self.blocks.values()]

    def get_block(self, block_id, version=0):
        self.fetches.append(block_id)
        b = self.blocks.get(block_id)
        if b is None:
            return None
        meta = BlockMeta(**dict(b.meta.__dict__))
        return Block(meta, b.text)


class TenantPolicy:
    def __init__(self, tenant):
        self.tenant = tenant

    def can(self, identity, action, target):
        t = getattr(target, "tenant_id", "")
        return (not t) or t == self.tenant


class AllowAll:
    """单用户产品的合法实现：没有角色体系，鉴权恒真。"""

    def can(self, identity, action, target):
        return True


def blk(bid, kind, text, **kw):
    meta = BlockMeta(id=bid, kind=kind, **kw)
    b = Block(meta, text)
    meta.content_hash = content_hash(text)
    return b


class AssemblyTests(unittest.TestCase):
    def test_01_kind_order(self):
        ctx = FakeContext([
            blk("p", "profile", "偏好：先给结论"),
            blk("s", "semantic", "事实：库是 gongxiang_dev"),
            blk("r", "procedural", "口径：金额不含退款"),
        ])
        out = assemble(context=ctx)
        self.assertEqual([b["id"] for b in out["blocks"]], ["r", "s", "p"])
        self.assertTrue(out["text"].startswith("口径"))
        self.assertEqual([derive_authority(b["kind"]) for b in out["blocks"]],
                         ["MUST", "IS", "WANT"])

    def test_02_budget_truncate(self):
        ctx = FakeContext([
            blk("r", "procedural", "规" * 60),
            blk("s", "semantic", "事" * 60),
            blk("p", "profile", "偏" * 60),
        ])
        out = assemble(context=ctx, max_total_chars=120)
        self.assertEqual(out["blocks"][0]["id"], "r")
        self.assertIn("p", out["dropped"])
        reasons = {t["id"]: t.get("reason") for t in out["trace"] if t.get("dropped")}
        self.assertEqual(reasons.get("p"), "budget")
        self.assertLessEqual(out["used_chars"], 130)

    def test_03_dedupe(self):
        same = blk("a", "semantic", "同一段话")
        dup_id = blk("a", "semantic", "同一段话")
        dup_text = blk("b", "semantic", "同一段话")
        out = assemble(context=FakeContext([same, dup_id, dup_text]))
        self.assertEqual([b["id"] for b in out["blocks"]], ["a"])

    def test_04_scope_isolation(self):
        ctx = FakeContext([
            blk("mine", "semantic", "本租户", tenant_id="t1"),
            blk("other", "semantic", "别租户", tenant_id="t2"),
        ])
        out = assemble(context=ctx, identity={"tenant_id": "t1"},
                       policy=TenantPolicy("t1"))
        self.assertEqual([b["id"] for b in out["blocks"]], ["mine"])
        self.assertIn("other", out["dropped"])

    def test_05_session_binding(self):
        recs = [
            MemoryRecord(kind="semantic", scope="session", session_id="s1",
                         content="群 1 的共识"),
            MemoryRecord(kind="semantic", scope="project", session_id="",
                         content="跨会话的项目事实"),
        ]
        s1 = select_records(recs, {"session_id": "s1"})
        s2 = select_records(recs, {"session_id": "s2"})
        # 同 kind 内 project 在 session 之前（scope 次序），session 只在本文会话可见
        self.assertEqual([r.content for r in s1], ["跨会话的项目事实", "群 1 的共识"])
        self.assertEqual([r.content for r in s2], ["跨会话的项目事实"])

    def test_06_profile_last(self):
        ctx = FakeContext([
            blk("p", "profile", "先给结论"),
            blk("s", "semantic", "决定用 A"),
        ])
        out = assemble(context=ctx)
        self.assertEqual([b["id"] for b in out["blocks"]], ["s", "p"])

    def test_07_superseded(self):
        old = MemoryRecord(kind="semantic", subtype="decision", key="方案",
                           content="用 A", status="superseded")
        new = MemoryRecord(kind="semantic", subtype="decision", key="方案",
                           content="用 B")
        got = select_records([old, new], {})
        self.assertEqual([r.content for r in got], ["用 B"])

    def test_08_expired(self):
        rec = MemoryRecord(kind="semantic", content="过期事实",
                           expires_at=time.time() - 10)
        self.assertEqual(select_records([rec], {}), [])

    def test_09_trace(self):
        ctx = FakeContext([blk("r", "procedural", "规则")])
        out = assemble(context=ctx)
        row = [t for t in out["trace"] if t.get("id") == "r"][0]
        for key in ("kind", "authority", "chars", "est_tokens", "version",
                    "content_hash", "cache"):
            self.assertIn(key, row)
        self.assertEqual(row["authority"], "MUST")

    def test_10_version_invalidate(self):
        ctx = FakeContext([blk("r", "procedural", "旧规则")])
        cache = BlockCache()
        first = assemble(context=ctx, cache=cache)
        self.assertEqual(ctx.fetches, ["r"])
        second = assemble(context=ctx, cache=cache)
        self.assertEqual(ctx.fetches, ["r"])           # 命中缓存，不重取
        self.assertEqual([t["cache"] for t in second["trace"] if t.get("id") == "r"],
                         ["hit"])
        # 内容变了（content_hash 变），即使忘了升 version 也要重取
        ctx.blocks["r"] = blk("r", "procedural", "新规则")
        third = assemble(context=ctx, cache=cache)
        self.assertEqual(ctx.fetches, ["r", "r"])
        self.assertIn("新规则", third["text"])
        self.assertNotIn("旧规则", third["text"])
        self.assertEqual(first["text"].strip(), "旧规则")

    def test_11_empty_sets(self):
        out = assemble(context=FakeContext([]))
        self.assertEqual(out["text"], "")
        self.assertEqual(out["blocks"], [])
        self.assertEqual(out["used_chars"], 0)

    def test_13_hash_only_version(self):
        """没有自增版本号的产品：version=0，用 content_hash 当版本。"""
        ctx = FakeContext([blk("g", "procedural", "标注规范 v1", version=0)])
        cache = BlockCache()
        assemble(context=ctx, cache=cache)
        second = assemble(context=ctx, cache=cache)
        self.assertEqual(ctx.fetches, ["g"])             # 第二轮命中
        self.assertEqual([t["cache"] for t in second["trace"] if t.get("id") == "g"],
                         ["hit"])
        ctx.blocks["g"] = blk("g", "procedural", "标注规范 v2", version=0)
        third = assemble(context=ctx, cache=cache)
        self.assertEqual(ctx.fetches, ["g", "g"])        # hash 变 → 重取
        self.assertIn("v2", third["text"])
        self.assertNotIn("v1", third["text"])

    def test_12_episodic_empty(self):
        """没有 episodic（批量标注场景）仍能拼装，且不补默认摘要。"""
        ctx = FakeContext([blk("r", "procedural", "标注规范 v3")])
        out = assemble(context=ctx, identity={"user_id": "u1"},
                       policy=AllowAll())
        self.assertEqual([b["id"] for b in out["blocks"]], ["r"])
        self.assertEqual(out["text"].strip(), "标注规范 v3")
        self.assertNotIn("摘要", out["text"])

    def test_24_cold_start_definition_only(self):
        """冷启动：记忆为空，定义层（规范）仍能进上下文。"""
        ctx = FakeContext([blk("policy", "procedural", "报销需审批")])
        out = assemble(context=ctx, identity={"user_id": "u1"})
        self.assertEqual([b["id"] for b in out["blocks"]], ["policy"])
        self.assertIn("报销需审批", out["text"])
        self.assertEqual(out["used_chars"], len("报销需审批"))


class ScopeRulesTests(unittest.TestCase):
    def test_14_semantic_scope_order(self):
        """语义事实：项目比平台更具体，项目优先。"""
        recs = [
            MemoryRecord(kind="semantic", scope="platform", key="k1", content="平台事实"),
            MemoryRecord(kind="semantic", scope="project", key="k2", content="项目事实"),
        ]
        got = select_records(recs, {})
        self.assertEqual([r.content for r in got], ["项目事实", "平台事实"])

    def test_15_procedural_scope_order(self):
        """规则类：平台制度压项目规则。"""
        recs = [
            MemoryRecord(kind="procedural", scope="project", key="k1", content="项目规则"),
            MemoryRecord(kind="procedural", scope="platform", key="k2", content="平台规则"),
        ]
        got = select_records(recs, {})
        self.assertEqual([r.content for r in got], ["平台规则", "项目规则"])

    def test_16_same_key_shadowed(self):
        """同 kind 同 key 跨 scope：只注入一条，另一条 trace 可审计。"""
        recs = [
            MemoryRecord(kind="semantic", scope="platform", key="库", content="库=platform_x"),
            MemoryRecord(kind="semantic", scope="project", key="库", content="库=proj_y"),
        ]
        kept, shadowed = select_records(recs, {}, include_shadowed=True)
        self.assertEqual([r.content for r in kept], ["库=proj_y"])
        self.assertEqual([r.content for r in shadowed], ["库=platform_x"])

    def test_17_namespaced_subtype(self):
        """x- 前缀是自由标签，不改变 kind 派生的权威。"""
        rec = MemoryRecord(kind="semantic", subtype="x-dw-amount", content="口径")
        self.assertEqual(rec.subtype, "x-dw-amount")
        self.assertEqual(derive_authority(rec.kind), "IS")

    def test_20_condition_prevents_shadow(self):
        """同 key 不同条件（DEV/PROD）并存，不互相压制。"""
        recs = [
            MemoryRecord(kind="semantic", scope="project", key="项目库",
                         condition={"env": "DEV"}, content="库=dev_db"),
            MemoryRecord(kind="semantic", scope="project", key="项目库",
                         condition={"env": "PROD"}, content="库=prod_db"),
        ]
        kept, shadowed = select_records(recs, {}, include_shadowed=True)
        self.assertEqual(len(kept), 2)
        self.assertEqual(shadowed, [])

    def test_23_agent_id_in_uniqueness(self):
        """R1 含 agent_id：两个智能体同 key 不互相压制。"""
        recs = [
            MemoryRecord(kind="semantic", scope="session", scope_id="room:1",
                         agent_id="agent:a", key="承诺", content="A 的承诺"),
            MemoryRecord(kind="semantic", scope="session", scope_id="room:1",
                         agent_id="agent:b", key="承诺", content="B 的承诺"),
        ]
        kept = select_records(recs, {})
        self.assertEqual(len(kept), 2)

    def test_21_status_reason_preserved(self):
        rec = MemoryRecord(kind="semantic", content="x",
                           status="archived", status_reason="user_retracted")
        self.assertEqual(rec.status_reason, "user_retracted")

    def test_22_provenance_accumulates(self):
        rec = MemoryRecord(kind="semantic", content="x", source="user",
                           provenance=[{"source": "run:1"}, {"source": "run:2"}])
        self.assertEqual(len(rec.provenance), 2)
        self.assertEqual(rec.source, "user")

    def test_19_record_tenant_isolation(self):
        """I11：跨租户 0 命中。"""
        recs = [
            MemoryRecord(tenant_id="t1", kind="semantic", content="t1 的事实"),
            MemoryRecord(tenant_id="t2", kind="semantic", content="t2 的事实"),
        ]
        got = select_records(recs, {"tenant_id": "t1"})
        self.assertEqual([r.content for r in got], ["t1 的事实"])

    def test_18_hash_uses_injected_text(self):
        """content_hash 算注入文本：summary 变了，hash 就要变。"""
        a = MemoryRecord(kind="semantic", content="很长的原文", summary="摘要 A")
        b = MemoryRecord(kind="semantic", content="很长的原文", summary="摘要 B")
        self.assertNotEqual(a.content_hash, b.content_hash)


class ModelTests(unittest.TestCase):
    def test_authority_derivation(self):
        self.assertEqual(derive_authority("procedural"), "MUST")
        self.assertEqual(derive_authority("semantic"), "IS")
        self.assertEqual(derive_authority("profile"), "WANT")
        self.assertEqual(derive_authority("episodic"), "NOW")
        self.assertEqual(derive_authority("unknown"), "IS")

    def test_record_normalizes_enum(self):
        rec = MemoryRecord(kind="Semantic", scope="Project", status="ACTIVE",
                           subtype="DECISION", content="x")
        self.assertEqual((rec.kind, rec.scope, rec.status, rec.subtype),
                         ("semantic", "project", "active", "decision"))

    def test_invalid_subtype_dropped(self):
        rec = MemoryRecord(kind="semantic", subtype="preference", content="x")
        self.assertEqual(rec.subtype, "")

    def test_no_experience_kind(self):
        rec = MemoryRecord(kind="experience", content="x")
        self.assertEqual(rec.kind, "semantic")


if __name__ == "__main__":
    unittest.main()
